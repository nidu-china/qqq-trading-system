# QQQ 0DTE 交易系统

基于 QQQ 已收盘 K 线的状态机交易系统。支持三种策略模式：
`trend`（Opening Range Breakout 趋势跟踪）、`boll_macd`（分时 BOLL/MACD 做 T）
和 `hybrid`（自动切换），通过 `.env` 中 `STRATEGY_MODE` 选择。
Paper、Live 和 Replay 共用同一套策略、VIX 过滤、选约和风控逻辑，详见
[STRATEGY.md](STRATEGY.md)。

> 0DTE 期权风险极高。本项目不构成投资建议。首次部署必须使用 Paper 模式验证数据、时区、成交与恢复行为。

## 策略概览

所有策略只使用已收盘的 1 分钟常规交易时段 K 线。信号 K 线收盘后立即执行，
不等待下一根确认线；信号有效期 60 秒。

### Trend ORB（`STRATEGY_MODE=trend`）

- 09:30–09:40 ET：构建开盘区间（OR），不开仓。
- 09:40–11:30 ET：检测 OR 突破 + EMA/VWAP 对齐，可入场；每日限 1 个方向。
- 11:30–13:55 ET：不再生成新信号，管理已有仓位。
- 13:55 ET：强制清空全部仓位。
- 入场指标：EMA(9)、EMA(21)、VWAP。

### BOLL/MACD（`STRATEGY_MODE=boll_macd`）

- 09:30–09:35 ET：指标预热，绝对不开仓。
- 09:35–09:42 ET：开盘爆量策略，独立的 BOLL/价格/成交量信号。
- 09:42–09:45 ET：不再开仓，09:45 清空开盘仓位。
- 09:45–12:00 ET：主时段 BOLL/MACD 信号。
- 12:00–13:55 ET：不再开新仓，管理已有趋势底仓至反转或 13:55。
- 13:55 ET：强制清空全部仓位。
- 入场指标：BOLL(20,2)、MACD(8,17,9)、RSI(14)、20 根均量。

### 共用规则

- VIX：NORMAL 双向、RISK_OFF 仅 Put、RECOVERY 仅 Call、SHOCK 禁止开仓；数据不可用时记录警告并放行。
- VIX 最近 5 分钟趋势会把顺势方向量比门槛降低 10%，逆势方向提高 10%（仅 BOLL/MACD）。

固定风控：

- Paper 初始权益 10,000 美元；Live 从 Longbridge 获取实时权益。
- 单笔权利金不超过权益 50%，最多 10 张，每日最多开仓 5 次，平仓后冷却 3 分钟。
- 期权价格下跌 25% 止损。
- 日内权益亏损达到开盘权益 2% 后清仓并停机。
- +100% 减半并移止损至成本，+250% 清仓；期权最高浮盈至少达到 25% 后才启动 30% 回吐移动止盈。移动止盈触发时卖出约一半，剩余仓位跟随 BOLL 中轨或 MACD 反转。
- 持仓满 20 分钟仍亏损则退出。
- 每张合约每边手续费 1.50 美元；买入上限为信号 Ask +0.02。普通退出按 Bid 限价执行；25%止损等待1分钟K线结束确认，确认后优先使用市价卖单，部分成交的剩余仓位再用激进限价追单。

## 数据与回测

回测按 QQQ 1 分钟收盘事件推进，并使用与实盘相同的状态机和风控：

1. 若存在有效历史期权报价，入场使用信号后 60 秒内第一条 Ask，退出使用当时 Bid。
2. 若缺少期权报价，按实盘选约范围生成方向性 OTM 合约，并使用 0DTE Black-Scholes
   逐分钟重新定价；IV 依次取此前已观测期权报价反推值、当时 VIX、历史波动率和保守默认值。
   合成 Bid/Ask 使用美分档位和动态价差，Put 另计波动率偏斜。
   已用真实报价建仓后若某一分钟报价缺失，也只对缺口分钟模型补价；下一条真实报价出现时立即恢复使用真实 Bid。
3. 首个回测日期前加载历史 RTH 数据，用于指标、前日高低点和前收盘价预热。
4. 部分止盈记录为同一笔持仓的 `exit_legs`，不会重复计算交易次数。
5. 结果包含收益、胜率、利润因子、回撤、信号/拒绝原因、报价来源、定价模型、入场 IV、
   IV 来源、价差、模型补价分钟数、手续费、滑点和分段退出。

市场数据位于：

```text
data/market/
  bars/symbol=QQQ.US/date=YYYY-MM-DD/data.parquet
  bars/symbol=.VIX.US/date=YYYY-MM-DD/data.parquet
  candidate_option_quotes/date=YYYY-MM-DD/data.parquet
```

没有 `candidate_option_quotes` 时回测会自动使用合成期权，并将 `option_data_complete` 标记为 `false`。

## CLI 命令参考

> **Windows** 请将 `qqq-trader` 替换为 `.\.venv\Scripts\qqq-trader.exe`，路径分隔符改为 `\`。

### backfill — 拉取历史 K 线

在启动交易引擎或运行离线回测前，需先用此命令将历史数据从 Longbridge 下载并存入本地 Parquet 文件。

```bash
# 拉取 QQQ 1m/5m K 线 + VIX 5m/日线（最常用，运行回测前必须先执行）
qqq-trader backfill --start 2026-07-01 --end 2026-08-06

# 只拉 QQQ K 线，跳过 VIX
qqq-trader backfill --start 2026-07-01 --end 2026-08-06 --no-include-volatility

# 单独补拉 VIX（--no-include-volatility 防止递归重复拉 QQQ）
qqq-trader backfill --start 2026-07-01 --end 2026-08-06 \
  --symbol .VIX.US --no-include-volatility

# PowerShell
.\.venv\Scripts\qqq-trader.exe backfill --start 2026-07-01 --end 2026-08-06
```

拉取后数据写入（路径由 `.env` 中 `DATA_DIR` 决定）：

```
{DATA_DIR}/bars/symbol=QQQ.US/date=YYYY-MM-DD/1m.parquet
{DATA_DIR}/bars/symbol=QQQ.US/date=YYYY-MM-DD/5m.parquet
{DATA_DIR}/bars/symbol=.VIX.US/date=YYYY-MM-DD/5m.parquet
{DATA_DIR}/bars/symbol=.VIX.US/date=YYYY-MM-DD/day.parquet
```

### backtest — 命令行回测

对已下载的 K 线数据进行离线回测，结果以 JSON 输出到标准输出。Web 界面的回测任务队列使用同一套引擎，但提供图表和交易明细。

```bash
# 最小用法：只有 K 线，使用合成期权定价
qqq-trader backtest \
  --bars data/market/bars \
  --starting-equity 10000

# 加入 VIX 过滤（推荐，否则 VIX 门控信号全部被拒绝）
qqq-trader backtest \
  --bars data/market/bars \
  --volatility-bars data/market/bars \
  --volatility-daily-bars data/market/bars \
  --starting-equity 10000

# 使用真实期权报价（需要 candidate_option_quotes 目录）
qqq-trader backtest \
  --bars data/market/bars \
  --option-frames data/market/candidate_option_quotes \
  --volatility-bars data/market/bars \
  --volatility-daily-bars data/market/bars \
  --starting-equity 10000

# PowerShell（反引号续行，路径用 Windows 格式）
.\.venv\Scripts\qqq-trader.exe backtest `
  --bars data\market\bars `
  --volatility-bars data\market\bars `
  --volatility-daily-bars data\market\bars `
  --starting-equity 10000
```

`--bars` / `--volatility-bars` / `--volatility-daily-bars` 接受目录时会递归合并所有日期的 Parquet 文件；也可以传单个 `.parquet` 文件只回测特定日期。

输出示例（JSON 到 stdout）：

```json
{
  "starting_equity": "10000",
  "ending_equity": "10850.00",
  "net_pnl": "850.00",
  "return_rate": "0.085",
  "signals": 12,
  "trades": 5,
  "win_rate": "0.60",
  "profit_factor": "2.30",
  "max_drawdown": "-220.00",
  "rejected": { "signal_expired": 2, "stale_quote": 1 },
  "option_data_complete": false,
  "volatility_data_complete": true,
  "volatility_regimes": { "normal": 8, "elevated": 2, "unavailable": 2 },
  "warning": ["No option Bid/Ask frames supplied; Greeks synthetic pricing is used."]
}
```

### trade — 启动交易引擎

同时启动交易服务和 Web API 界面，二者共用同一进程。

```bash
qqq-trader trade                              # Linux
.\.venv\Scripts\qqq-trader.exe trade          # Windows
```

### report — 重新生成日报

从 MySQL 读取交易记录，结合本地 K 线重新生成指定日期的 HTML/Markdown/JSON/SVG 日报。

```bash
qqq-trader report --trading-date 2026-08-06
```

### reconcile — 对账检查

查询券商当前持仓并与引擎状态对比，判断是否可以安全启动。通常在服务异常退出后重启前执行。

```bash
qqq-trader reconcile
# 返回码 0 = 安全，返回码 2 = 有未处理持仓或状态不一致
```

## 本地运行（Windows）

要求 Python 3.12+、Node.js 和 MySQL。Windows PowerShell 示例：

```powershell
Copy-Item .env.example .env
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\alembic.exe upgrade head
.\.venv\Scripts\qqq-trader.exe api
```

前端：

```powershell
Set-Location frontend
pnpm install
pnpm run build
```

## Linux 部署

### 环境要求

- Python 3.12+
- Node.js 18+
- MySQL 8.0+（可使用 Docker 运行）

### 1. MySQL 数据库

如果已有 Docker 运行的 MySQL，直接创建数据库和用户即可；如果从零开始：

```bash
docker run -d \
  --name qqq-mysql \
  -p 3306:3306 \
  -e MYSQL_ROOT_PASSWORD=your-root-password \
  -e MYSQL_DATABASE=qqq \
  -e MYSQL_USER=qqq \
  -e MYSQL_PASSWORD=your-password \
  -v /data/app/mysql/data:/var/lib/mysql \
  --restart=always \
  mysql:8.0
```

> 注意：`MYSQL_ROOT_PASSWORD`、`MYSQL_DATABASE`、`MYSQL_USER`、`MYSQL_PASSWORD` 只在**首次初始化**（空数据目录）时生效。如果数据目录已有旧数据，需要手动进入 MySQL 创建库和用户。

### 2. 项目安装

```bash
cd /opt/qqq-trading-system
cp .env.example .env
nano .env  # 编辑所有必填配置（见下方说明）

python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# 建表
alembic upgrade head
```

### 3. 前端构建

```bash
cd frontend
pnpm install
pnpm run build
cd ..
```

构建后的静态文件由 FastAPI 直接 serve，无需额外的 Web 服务器。

### 4. 启动服务

```bash
source .venv/bin/activate

# 启动交易引擎 + Web 界面（两者共用同一进程）
qqq-trader trade
```

访问 `http://服务器IP:8000` 即可打开前端页面。

### 5. systemd 自启动（推荐）

```bash
sudo tee /etc/systemd/system/qqq-trader.service <<'EOF'
[Unit]
Description=QQQ Trading System
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/qqq-trading-system
EnvironmentFile=/opt/qqq-trading-system/.env
ExecStart=/opt/qqq-trading-system/.venv/bin/qqq-trader trade
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now qqq-trader
```

常用命令：

```bash
sudo systemctl status qqq-trader   # 查看状态
sudo journalctl -u qqq-trader -f   # 查看日志
sudo systemctl restart qqq-trader  # 重启
```

### 6. `.env` 关键配置

所有交易参数**必须**在 `.env` 中显式配置，没有隐含默认值。参考 `.env.example` 获取完整列表。

```dotenv
# 必填 —— 交易模式与账户
TRADING_MODE=paper          # paper / live
STRATEGY_MODE=trend         # trend / boll_macd / hybrid
ACCOUNT_ID=你的账户ID

# 必填 —— API 监听地址（Linux 部署务必设为 0.0.0.0）
API_HOST=0.0.0.0
API_PORT=8000

# 必填 —— 数据库（host 对应 MySQL 地址）
DATABASE_URL=mysql+asyncmy://qqq:your-password@127.0.0.1:3306/qqq?charset=utf8mb4

# 必填 —— Longbridge 凭证（Live 模式）
LONGBRIDGE_APP_KEY=
LONGBRIDGE_APP_SECRET=
LONGBRIDGE_ACCESS_TOKEN=
```

> **首次部署务必使用 `TRADING_MODE=paper`**，验证数据接收、信号产生、时区正确后再切换到 `live`。

### 7. Docker 部署（可选）

```bash
docker compose up --build
```

### 美股交易时间参考（夏令时）

| 美东时间 (ET) | 北京时间 (BJT) | 说明 |
|---|---|---|
| 09:30 开盘 | 21:30 | |
| 09:40 | 21:40 | Trend ORB：OR 构建结束，开始检测突破 |
| 11:30 | 23:30 | Trend ORB：停止新开仓 |
| 12:00 | 00:00 | BOLL/MACD：停止新开仓 |
| 13:55 强制平仓 | 次日 01:55 | 所有策略 |
| 16:00 收盘 | 次日 04:00 | |
| 16:15 生成日报 | 次日 04:15 | |

Live 模式除 Longbridge 凭证外，还必须设置：

```dotenv
TRADING_MODE=live
ACCOUNT_ID=你的账户ID
```

## 配置边界

所有交易参数**必须**在 `.env` 中显式配置，没有隐含默认值。参考 `.env.example` 获取完整列表。

- **在线配置页**：仅开放 VIX 波动率过滤参数的实时调整。
- **回测页面**：支持选择策略模式，并动态加载对应策略的全部可调参数进行微调回测。
- 交易时段、仓位、止盈止损、流动性、手续费和执行规则固定在 `STRATEGY.md` 和 `.env` 中。
- 枚举标签（退出原因、拒绝原因、VIX 状态）由后端 `/api/v1/labels` 统一提供，前端动态加载。

基础设施环境变量（账户凭证、数据库、目录、Longbridge、API、日志和调度）仍由 `.env` 管理。

## 验证

Windows：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\ruff.exe check src tests
Set-Location frontend
pnpm run build
```

Linux：

```bash
source .venv/bin/activate
pytest -q
ruff check src tests
cd frontend && pnpm run build
```

主要测试文件：

- `tests/test_strategy.py`：BOLL/MACD 指标、完整 1 分钟 K 线、即时信号和状态分类。
- `tests/test_trend_strategy.py`：Trend ORB 策略：OR 构建、突破确认、EMA/VWAP 退出。
- `tests/test_hybrid_strategy.py`：Hybrid 模式自动切换逻辑。
- `tests/test_risk.py`：选约、流动性、仓位、日亏损和所有固定退出规则。
- `tests/test_backtest.py`：合成报价、聚合退出、13:55 强平和取消回测。
- `tests/test_volatility.py`：VIX 五种状态及方向许可。
- `tests/test_execution_adapter.py`：订单追价、成交与适配器行为。
- `tests/test_configuration.py`：配置加载、校验和跨字段验证。
