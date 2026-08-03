# QQQ 0DTE 交易系统

基于 QQQ 已收盘 K 线的状态机交易系统。Paper、Live 和 Replay 共用同一套
分时 BOLL/MACD 策略、VIX 过滤、选约和风控逻辑，详见
[STRATEGY.md](STRATEGY.md)。

> 0DTE 期权风险极高。本项目不构成投资建议。首次部署必须使用 Paper 模式验证数据、时区、成交与恢复行为。

## 策略概览

- 09:30–09:35 ET：仅预热指标，绝对不开仓。
- 09:35–09:42 ET：允许 BOLL/MACD 爆量趋势信号。
- 09:42–09:45 ET：不再开仓，09:45 清空开盘仓位。
- 09:45–11:25 ET：允许产生新的 BOLL/MACD 信号。
- 11:25–11:30 ET：不再产生新信号。
- 11:30 ET：不再开新仓，剩余仓位向上取整减半，之后仅管理。
- 13:55 ET：强制清空全部仓位。
- 所有指标和信号都使用已收盘的 1 分钟常规交易时段 K 线。
- 信号 K 线收盘后立即执行，不等待下一根确认线；信号有效期 60 秒。
- VIX：NORMAL 双向、RISK_OFF 仅 Put、RECOVERY 仅 Call、SHOCK/UNAVAILABLE 禁止开仓。

入场指标为 BOLL(20,2)、MACD(8,17,9)、RSI(14) 和 20 根均量。

固定风控：

- Paper 初始权益 10,000 美元；Live 从 Longbridge 获取实时权益。
- 单笔权利金不超过权益 50%，最多 10 张，每日最多开仓 5 次，平仓后冷却 5 分钟。
- 期权价格下跌 25% 止损；QQQ 结构止损含 `0.1×ATR` 缓冲，距离超过 `2×ATR` 拒绝入场。
- 日内权益亏损达到开盘权益 2% 后清仓并停机。
- +100% 减半并移止损至成本，+250% 清仓；最高浮盈回吐 30% 时全部止盈。
- 持仓满 20 分钟仍亏损则退出。
- 每张合约每边手续费 1.50 美元；买入上限为信号 Ask +0.02，卖出按 Bid 并计入 0.02 报价滑点。

## 数据与回测

回测按 QQQ 1 分钟收盘事件推进，并使用与实盘相同的状态机和风控：

1. 若存在有效历史期权报价，入场使用信号后 60 秒内第一条 Ask，退出使用当时 Bid。
2. 若缺少期权报价，使用最近整数 ATM 合约和文档规定的 Delta/Gamma/Theta 合成报价。
3. 首个回测日期前加载历史 RTH 数据，用于指标、前日高低点和前收盘价预热。
4. 部分止盈记录为同一笔持仓的 `exit_legs`，不会重复计算交易次数。
5. 结果包含收益、胜率、利润因子、回撤、信号/拒绝原因、报价来源、手续费、滑点和分段退出。

市场数据位于：

```text
data/market/
  bars/symbol=QQQ.US/date=YYYY-MM-DD/data.parquet
  bars/symbol=.VIX.US/date=YYYY-MM-DD/data.parquet
  candidate_option_quotes/date=YYYY-MM-DD/data.parquet
```

没有 `candidate_option_quotes` 时回测会自动使用合成期权，并将 `option_data_complete` 标记为 `false`。

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
npm install
npm run build
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
npm install
npm run build
cd ..
```

构建后的静态文件由 FastAPI 直接 serve，无需额外的 Web 服务器。

### 4. 启动服务

```bash
source .venv/bin/activate

# 只启动 Web 界面（K 线、回测、信号查看）
qqq-trader api

# 启动完整交易引擎 + Web 界面
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

| 美东时间 (ET) | 北京时间 (BJT) |
|---|---|
| 09:30 开盘 | 21:30 |
| 11:30 停止新开仓 | 23:30 |
| 13:55 强制平仓 | 次日 01:55 |
| 16:00 收盘 | 次日 04:00 |
| 16:15 生成日报 | 次日 04:15 |

Live 模式除 Longbridge 凭证外，还必须设置：

```dotenv
TRADING_MODE=live
ACCOUNT_ID=你的账户ID
```

## 配置边界

在线配置页和回测自定义参数只开放技术指标及 VIX 字段。交易时段、仓位、止盈止损、流动性、手续费和执行规则固定在 `STRATEGY.md`，旧配置版本中的 R 风险、百分比追价、固定行权价偏移等字段会被静默忽略。

基础设施环境变量（账户凭证、数据库、目录、Longbridge、API、日志和调度）仍由 `.env` 管理。

## 验证

Windows：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\ruff.exe check src tests
Set-Location frontend
npm run build
```

Linux：

```bash
source .venv/bin/activate
pytest -q
ruff check src tests
cd frontend && npm run build
```

主要测试文件：

- `tests/test_strategy.py`：指标、OR、完整1分钟K线、即时信号和状态分类。
- `tests/test_risk.py`：选约、流动性、仓位、日亏损和所有固定退出规则。
- `tests/test_backtest.py`：合成报价、聚合退出、13:55 强平和取消回测。
- `tests/test_volatility.py`：VIX 五种状态及方向许可。
- `tests/test_execution_adapter.py`：订单追价、成交与适配器行为。
