# QQQ 0DTE 量化交易系统

这是一个使用 Python 3.12 开发的 QQQ 当日到期（0DTE）期权交易系统。系统根据
QQQ 的完整5分钟K线判断 Call/Put 方向，并结合 VIX 波动率状态过滤风险。

## 交易模式

系统支持两种实时运行模式，历史回测内部使用 `replay`：

| 模式 | 配置 | 行为 |
| --- | --- | --- |
| Paper（默认） | `TRADING_MODE=paper` | 先记录买入/卖出信号，再由PaperBroker模拟成交、持仓和止盈止损 |
| 实盘 | `TRADING_MODE=live` | 使用Longbridge真实账户下单，必须完成双重确认和期权权限检查 |

两种模式遵循完全相同的信号和执行流程：

```text
策略评估(MACD+布林带+RSI+成交量) → VIX波动率过滤 → 选约+风控
→ 持久化可执行交易信号(TradeSignal) → 提交订单(PaperBroker/Longbridge)
→ 订单状态跟踪 → 持仓管理(止盈/止损/强平)
→ 持久化卖出信号 → 提交卖出订单 → 写入成交摘要
```

区别仅在于订单提交到 PaperBroker 还是真实券商。

> 0DTE期权可能在极短时间内损失全部权利金。本项目不保证盈利。首次使用请保持
> `TRADING_MODE=paper`，完成足够长时间的数据采集、回测和故障演练后再考虑实盘。

如果已经完成依赖安装、`.env` 配置和 MySQL 建库，日常启动只需要：

```powershell
cd D:\Workspace\personal\qqq-trading-system
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\qqq-trader.exe trade
```

然后在另一个终端检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health/ready
```

Docker 构建或前端生产构建完成后，可打开 `http://127.0.0.1:8000` 使用量化管理界面。
“信号对比”页面展示持久化的买入、卖出决策，并可选择历史回测任务比较同一5分钟K线的
状态和拒绝原因。对应接口为 `GET /api/v1/signals`。Paper与实盘遵循相同的
“策略信号 → 可执行交易信号 → 订单 → 成交”顺序，区别仅在于订单提交到PaperBroker还是真实券商。

## 1. 默认Paper模式做什么

默认模式会模拟完整的期权交易生命周期，不会向真实券商下单：

```text
Longbridge实时订阅 QQQ.US 1分钟K线
Longbridge实时订阅 .VIX.US 1分钟K线
                    ↓
          本地保存1分钟K线
                    ↓
     MACD + 布林带突破 + 成交量 + RSI（基于1分钟K线）
                    ↓
             VIX风险状态过滤
                    ↓
持久化 Call/Put 买入信号和指标快照
                    ↓
      选约、风控并持久化可执行买入信号
                    ↓
             PaperBroker模拟买入
                    ↓
      止盈/止损触发时先记录卖出信号再卖出
                    ↓
          写入MySQL事件和Parquet
```

终端信号示例：

```text
PAPER BUY SIGNAL | CALL | QQQ...C...US | QTY=2 | REF=1.25 | REASON=entry_call
```

Paper模式会调用期权链和期权报价接口，因此需要OpenAPI美股期权行情权限。信号仍然只会在
美东时间09:45–12:00、完整1分钟K线收盘后产生，同一根K线只处理一次，完全平仓后冷却5分钟，
每个交易日最多5次开仓。

## 2. 项目目录

```text
qqq-trading-system/
  src/qqq_trader/              Python主程序
    adapters/longbridge.py     Longbridge行情、K线、期权和交易适配器
    adapters/paper.py          虚拟成交券商
    strategy.py                MACD/布林带/成交量/RSI策略
    volatility.py              VIX状态分类和方向过滤
    engine.py                  信号、风控、下单和状态机
    service.py                 实时K线、存储、日报调度
    backtest.py                事件驱动回测
    backtest_service.py        管理页面的后台回测任务
    persistence.py             MySQL模型和Parquet存储
    reporting.py               Markdown/HTML/JSON日报
    api.py                     健康检查和管理API
    cli.py                     qqq-trader命令入口
  frontend/                    Vue 3管理页面
  migrations/                  Alembic数据库迁移
  sql/mysql_schema.sql         全新MySQL数据库的完整建表脚本
  tests/                       自动化测试，不是生产启动脚本
  data/market/                 本地行情数据（运行后生成）
  data/reports/                本地日报（运行后生成）
  .env.example                 配置模板
  .env                         本地私密配置，不应提交
```

## 3. Windows + VS Code首次安装

### 3.1 环境要求

- Windows 10/11
- Python 3.12（不要使用3.13或更旧版本创建此项目的虚拟环境）
- MySQL 8.x
- Node.js 22（只有开发管理页面时才需要）
- Longbridge OpenAPI凭证

在项目根目录打开VS Code，然后打开PowerShell终端：

```powershell
cd D:\Workspace\personal\qqq-trading-system
```

### 3.2 创建虚拟环境

```powershell
py -3.12 -m venv .venv
```

不需要激活虚拟环境，后续直接使用完整路径，能避免VS Code选择了错误的Python：

```powershell
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

这里使用 `-e ".[dev]"` 是因为项目以 `pyproject.toml` 作为唯一依赖清单：

- `-e`：以可编辑方式安装，修改 `src/` 后无需反复安装。
- `[dev]`：同时安装pytest、pytest-asyncio、Ruff等开发工具。
- 安装后会生成 `.venv\Scripts\qqq-trader.exe`。

检查安装：

```powershell
.\.venv\Scripts\python.exe --version
.\.venv\Scripts\qqq-trader.exe --help
```

### 3.3 配置.env

```powershell
Copy-Item .env.example .env
```

本地Paper模式最重要的配置如下：

```dotenv
TRADING_MODE=paper

DATABASE_URL=mysql+asyncmy://qqq:你的密码@127.0.0.1:3306/qqq?charset=utf8mb4
DATA_DIR=./data/market
REPORT_DIR=./data/reports

LONGBRIDGE_APP_KEY=你的AppKey
LONGBRIDGE_APP_SECRET=你的AppSecret
LONGBRIDGE_ACCESS_TOKEN=你的AccessToken

VOLATILITY_FILTER_ENABLED=true
VOLATILITY_SYMBOL=.VIX.US
```

也可以填写 `LONGBRIDGE_CLIENT_ID`使用OAuth。如果配置了Client ID，程序优先走OAuth；
否则使用三项API Key凭证。程序优先读取启动目录中的 `.env`，editable install时也会回退
查找项目根目录的 `.env`。

不要把真实凭证复制到README、测试文件或提交到Git。

### 3.4 准备MySQL

如果数据库和用户已经存在，只需确认 `.env`中的 `DATABASE_URL`可以连接。

全新本地MySQL可以先创建用户：

```sql
CREATE DATABASE IF NOT EXISTS qqq CHARACTER SET utf8mb4;
CREATE USER IF NOT EXISTS 'qqq'@'localhost' IDENTIFIED BY 'change-me';
GRANT ALL PRIVILEGES ON qqq.* TO 'qqq'@'localhost';
FLUSH PRIVILEGES;
```

已有数据库使用Alembic升级：

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
```

检查当前迁移版本：

```powershell
.\.venv\Scripts\python.exe -m alembic current
.\.venv\Scripts\python.exe -m alembic heads
```

全新数据库也可以直接执行 [sql/mysql_schema.sql](sql/mysql_schema.sql)。完整建表脚本和
Alembic二选一完成首次建库即可，后续结构升级统一使用Alembic。

## 4. 启动服务

### 4.1 启动后端和实时信号服务

确认MySQL已启动，然后在项目根目录执行：

```powershell
.\.venv\Scripts\qqq-trader.exe trade
```

等价的Python模块启动方式：

```powershell
.\.venv\Scripts\python.exe -m qqq_trader.cli trade
```

启动成功后，Paper模式会显示：

```text
PAPER MODE | REAL-TIME 1M CANDLES | QQQ.US, .VIX.US
```

在美股交易窗口内命中策略时才会出现 `PAPER BUY SIGNAL`。没有输出买入信号不代表
服务没有运行，可能只是当前不在09:45–12:00美东窗口或策略条件未同时满足。
策略使用1分钟K线评估，需要至少21根完整K线预热（约21分钟），开盘后最早约09:51可能产生信号。

停止服务使用 `Ctrl+C`。

### 4.2 检查服务状态

新开一个PowerShell终端：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health/live
Invoke-RestMethod http://127.0.0.1:8000/health/ready
Invoke-RestMethod http://127.0.0.1:8000/status
```

如果配置了 `API_TOKEN`，需对 `/status` 等非公开端点附带 token：

```powershell
$headers = @{ "Authorization" = "Bearer 你的API_TOKEN" }
Invoke-RestMethod http://127.0.0.1:8000/status -Headers $headers
```

常用地址：

| 地址 | 作用 |
| --- | --- |
| `http://127.0.0.1:8000/health/live` | Python进程是否存活 |
| `http://127.0.0.1:8000/health/ready` | 交易引擎是否为READY |
| `http://127.0.0.1:8000/status` | 模式、信号计数、VIX状态、错误原因 |
| `http://127.0.0.1:8000/metrics` | Prometheus指标 |
| `http://127.0.0.1:8000/docs` | FastAPI接口文档 |

如果状态为 `HALTED`，先查看 `/status`中的 `last_error`，不要反复重启掩盖错误。

### 4.3 启动管理页面

开发模式需要两个终端：后端保持 `qqq-trader.exe trade`运行，另一个终端执行：

```powershell
cd frontend
& 'D:\Software\node-v22.22.3-win-x64\npm.cmd' install
& 'D:\Software\node-v22.22.3-win-x64\npm.cmd' run dev
```

如果系统中的 `npm`可以直接执行，也可以使用：

```powershell
npm install
npm run dev
```

打开 `http://127.0.0.1:5173`。Vite会把 `/api`、`/health`和`/status`代理到8000端口。

如果希望后端8000端口直接提供管理页面，先构建：

```powershell
cd frontend
npm run build
cd ..
.\.venv\Scripts\qqq-trader.exe trade
```

构建后打开 `http://127.0.0.1:8000`。

## 5. 策略规则

方向判断只使用QQQ，不使用期权价格产生方向信号：

### Call信号

- 完整1分钟K线收盘。
- `MACD(5,10,3)`的MACD线高于信号线。
- 收盘价从布林带内向上突破 `Bollinger(20,2)`上轨。
- 当前成交量大于此前20根完整K线均量的1.2倍。
- `RSI(14) < 70`。
- VIX状态允许Call。

### Put信号

- MACD方向、布林带下轨突破与Call完全镜像。
- `RSI(14) > 30`。
- VIX状态允许Put。

### VIX过滤

- Normal：允许原始Call或Put信号。
- Risk-off：只允许Put。
- Recovery：只允许Call。
- Shock：Call和Put都禁止。
- 数据缺失或过期：安全拒绝信号。

Paper和实盘都会按 `QQQ现价+2`的Call目标行权价或 `QQQ现价-2`的Put目标
行权价查询当日期权链，并选择距离最近且通过流动性风控的真实合约。

## 6. 行情回填

按天拉取QQQ 1分钟K线、派生5分钟K线，并同时拉取VIX：

```powershell
.\.venv\Scripts\qqq-trader.exe backfill --start 2026-07-15 --end 2026-07-21
```

只拉QQQ：

```powershell
.\.venv\Scripts\qqq-trader.exe backfill `
  --start 2026-07-15 `
  --end 2026-07-21 `
  --no-include-volatility
```

程序逐日请求，避免Longbridge单次最多返回1000根历史K线造成跨日数据截断。周末和
尚未开盘的日期返回0根属于正常情况。

## 7. 回测

### 7.1 单组参数回测

```powershell
.\.venv\Scripts\qqq-trader.exe backtest `
  --bars "data\market\bars\symbol=QQQ.US" `
  --option-frames "data\market\candidate_option_quotes\symbol=QQQ.US" `
  --volatility-bars "data\market\bars\symbol=.VIX.US" `
  --volatility-daily-bars "data\market\bars\symbol=.VIX.US"
```

`--bars` 指向包含1分钟K线的目录（`1m.parquet` 文件）。

### 7.2 比较MACD组合

`.env`默认候选参数：

```dotenv
MACD_BACKTEST_COMBINATIONS=8,17,9;6,13,5;5,10,3
```

运行：

```powershell
.\.venv\Scripts\qqq-trader.exe backtest `
  --bars "data\market\bars\symbol=QQQ.US" `
  --option-frames "data\market\candidate_option_quotes\symbol=QQQ.US" `
  --volatility-bars "data\market\bars\symbol=.VIX.US" `
  --volatility-daily-bars "data\market\bars\symbol=.VIX.US" `
  --compare-macd
```

系统按净收益、再按最大回撤排序，并输出胜率、盈亏比、交易次数和数据完整性。

只有QQQ K线而没有历史0DTE期权Bid/Ask时，只能研究信号，不能得出真实期权胜率或
收益率。数据不完整的结果不会被选为“最佳参数”。

## 8. 日报

从MySQL成交记录和Parquet重新生成指定日期日报：

```powershell
.\.venv\Scripts\qqq-trader.exe report --trading-date 2026-07-15
```

输出目录：

```text
data/reports/YYYY-MM-DD/
  report.md
  report.html
  report.json
  qqq.svg
```

查看Paper信号：

```sql
SELECT created_at, message, details
FROM system_events
WHERE kind = 'paper_buy_signal'
ORDER BY id DESC;
```

## 9. 自动化测试怎么用

`tests/`中的文件是开发阶段自动验证程序，不需要在启动服务时逐个运行，也不会连接
Longbridge真实账户或发送真实订单。修改策略、风控、数据库或执行逻辑后，应运行对应测试；
提交或实盘前运行全部测试。

### 9.1 运行全部测试

```powershell
.\.venv\Scripts\python.exe -m pytest
```

显示测试名称和更详细结果：

```powershell
.\.venv\Scripts\python.exe -m pytest -v
```

### 9.2 每个测试文件的作用

| 文件 | 验证内容 | 什么时候运行 |
| --- | --- | --- |
| `tests/test_strategy.py` | EMA/MACD、布林带、RSI、放量过滤、Call/Put突破、1m转5m | 修改策略或K线聚合后 |
| `tests/test_volatility.py` | VIX normal/risk-off/recovery/shock和数据缺失时的方向限制 | 修改VIX阈值或过滤逻辑后 |
| `tests/test_risk.py` | 现价±2选约、流动性、仓位、止损、分批止盈、日亏损熔断 | 修改风控参数或退出规则后 |
| `tests/test_engine.py` | 状态机、Paper/实盘统一信号顺序、虚拟成交、VIX拒绝、在线配置延迟应用 | 修改engine、运行模式或订单流程后 |
| `tests/test_execution_adapter.py` | 部分成交追价、Longbridge SDK参数、历史请求超时、逐日回填 | 修改Longbridge或订单适配器后 |
| `tests/test_backtest.py` | 回测使用Ask入场、可成交Bid退出，避免未来数据 | 修改回测成交模型后 |
| `tests/test_persistence_reporting.py` | Parquet幂等写入、manifest、三种日报和图表 | 修改存储或日报后 |
| `tests/test_configuration.py` | 敏感字段不可在线修改、跨字段校验、MACD组合解析 | 修改配置项后 |
| `tests/test_api_cli.py` | 健康检查、状态、指标、API错误结构和CLI命令是否存在 | 修改API或CLI后 |
| `tests/conftest.py` | 所有测试共享的确定性QQQ 5分钟K线夹具，本身不单独运行 | 测试行情样本需要调整时 |

### 9.3 单独运行某类测试

```powershell
# 只测试策略
.\.venv\Scripts\python.exe -m pytest tests\test_strategy.py -v

# 只测试Paper模式和交易引擎
.\.venv\Scripts\python.exe -m pytest tests\test_engine.py -v

# 只运行一个测试函数
.\.venv\Scripts\python.exe -m pytest `
  tests\test_engine.py::test_paper_publishes_buy_signal_before_order_and_executes -v
```

### 9.4 静态检查和覆盖率

```powershell
.\.venv\Scripts\python.exe -m ruff check src tests migrations
.\.venv\Scripts\python.exe -m pytest --cov=qqq_trader --cov-report=term-missing
```

如果出现 `No module named pytest`，说明只安装了运行依赖，请重新执行：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

## 10. 常见问题

### qqq-trader.exe找不到

确认执行的是项目虚拟环境中的文件：

```powershell
Get-Item .\.venv\Scripts\qqq-trader.exe
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

也可以直接使用：

```powershell
.\.venv\Scripts\python.exe -m qqq_trader.cli trade
```

### .env没有加载

启动时程序会读取当前目录的 `.env`；editable install还会回退到项目根目录。可以安全检查
非敏感配置：

```powershell
.\.venv\Scripts\python.exe -c "from qqq_trader.config import Settings; s=Settings(); print(s.trading_mode, s.database_url, s.data_dir)"
```

不要打印App Secret或Access Token。

### MySQL Access denied

检查 `.env`中的用户名、URL编码后的密码、主机和数据库名。本地运行通常使用
`127.0.0.1`，不是Docker服务名 `mysql`：

```dotenv
DATABASE_URL=mysql+asyncmy://qqq:change-me@127.0.0.1:3306/qqq?charset=utf8mb4
```

### 服务启动但没有信号

依次检查：

1. `/health/ready`是否为 `true`。
2. `/status`中的状态是否为 `ready`，`last_error`是否为空。
3. 当前是否为美股交易日和美东09:45–12:00。
4. 默认 `MACD(5,10,3)` 至少需要21根完整1分钟K线完成全部指标预热；如果切换到
   `MACD(8,17,9)`，至少需要25根。
5. VIX历史是否足够且没有进入shock状态。
6. MACD、布林带突破、成交量和RSI是否同时满足。

没有信号时系统不会为了测试而制造假信号。

### Longbridge提示没有USOption OpenAPI权限

Paper和实盘都需要单独开通OpenAPI美股期权行情权限；Longbridge App中的行情权限不能替代
OpenAPI权限。缺少权限时启动检查会进入 `HALTED`，不会退化为只发信号模式。

### backfill返回的数据少于预期

周末、节假日和当日尚未开盘都不会产生QQQ K线。程序已经按日期拆分请求，避免1000根
上限截断，并使用Parquet主键幂等合并，重复回填不会制造重复K线。

## 11. 可选Docker启动

本地VS Code开发不要求Docker。如果需要容器化运行：

```powershell
docker compose up --build -d
docker compose logs -f trader
```

停止：

```powershell
docker compose down
```

Docker卷保存MySQL、行情和日报；普通 `docker compose down`不会删除卷，不要随意使用
`docker compose down -v`。

## 12. 实盘安全开关

实盘必须同时配置：

```dotenv
TRADING_MODE=live
ACCOUNT_ID=你的账户ID
LIVE_TRADING_ACK=I_UNDERSTAND_LIVE_TRADING:你的账户ID
LONGBRIDGE_CLIENT_ID=你的OAuth客户端ID
```

实盘还要求期权交易权限、OpenAPI OPRA行情、美元资金、交易日、行情新鲜度、数据库可写、
任一检查失败都会进入 `HALTED`，系统不会通过试探订单检测权限。

### 管理API安全

默认 `API_HOST=127.0.0.1` 仅监听本地回环地址。如需在局域网提供访问，应同时配置
`API_TOKEN`：

```dotenv
API_HOST=0.0.0.0
API_TOKEN=一个足够长的随机字符串
```

设置 `API_TOKEN` 后，除 `/health/live`、`/health/ready` 和 `/metrics` 外所有端点
需携带 `Authorization: Bearer <token>` 头部。

### 启动恢复机制

服务重启时执行以下恢复步骤（按顺序）：

1. **Preflight 检查** — 资金、保证金、交易日等条件全部通过后才继续。检查失败时直接
   `HALTED`，不会修改任何券商订单或信号状态。
2. **每日限额恢复** — 从数据库查询当日已成交记录，恢复 `realized_pnl` 和
   `trades_today`，`opening_equity = 当前权益 - 当日已实现盈亏`，确保重启不会绕过
   日亏损熔断或交易次数限制。
3. **订单和持仓处理** — 带有本系统 `intent_id`且能匹配 `trade_signals`的活动订单
   会先撤销并记录最终状态。
4. **持仓接管** — 券商中恰好一笔、且标的/方向/数量及数据库累计成交净数量都能匹配
   已持久化买入信号的持仓会被自动接管。引擎随后直接进入 `OPEN`并继续执行止盈、
   止损和强制平仓。
5. **券商订单回填** — 查询券商当日所有终态订单（已成交、已取消等），回写数据库以
   补全崩溃窗口中遗漏的记录。
6. **成交摘要重建** — 对于有 BUY+SELL 信号均已执行但缺少 `trade_summaries` 记录
   的交易对，自动从订单记录重建成交摘要，确保盈亏统计完整。
7. **信号状态修复** — 没有对应订单或持仓的遗留 `accepted`信号标记为 `failed`，
   不会在重启后补发旧订单。
8. **安全退回** — 无法匹配的订单或持仓、多个持仓、撤单未确认仍会进入 `HALTED`，
   避免误接管账户资产。

### reconcile 命令

```powershell
.\.venv\Scripts\qqq-trader.exe reconcile
```

该命令只读取券商状态并报告引擎是否可以安全就绪，**不会提交任何买入或卖出订单**。
如果券商中仍有持仓或订单，会报告 `HALTED` 并以退出码 2 结束。

状态机：

```text
STARTING -> READY -> ENTRY_PENDING -> OPEN -> EXIT_PENDING -> READY
     \______________________________________________________-> HALTED
```

`HALTED`不会自动解除。处理原因后使用 `reconcile` 检查是否可以重新开始。
