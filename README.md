# QQQ 0DTE 交易系统

基于 QQQ 已收盘 K 线的状态机交易系统。Paper、Live 和 Replay 共用同一套
VIX 过滤、选约和风控逻辑。默认动态结构策略见 [STRATEGY.md](STRATEGY.md)，
可选的分时趋势做 T 策略见 [TIMED_TREND_STRATEGY.md](TIMED_TREND_STRATEGY.md)。

> 0DTE 期权风险极高。本项目不构成投资建议。首次部署必须使用 Paper 模式验证数据、时区、成交与恢复行为。

## 策略概览

- 09:30–09:44 ET：仅观察并预热动态价格结构和技术指标，不开仓。
- 09:45–11:29 ET：按“反转 > 趋势 > 震荡”的优先级寻找机会。
- 11:30 ET：不再开新仓，剩余仓位向上取整减半，之后仅管理。
- 13:55 ET：强制清空全部仓位。
- 趋势、反转和震荡均在每根完整 1 分钟线收盘时判定并立即执行。
- 信号 K 线收盘后立即执行，不等待下一根确认线；信号有效期 60 秒。
- VIX：NORMAL 双向、RISK_OFF 仅 Put、RECOVERY 仅 Call、SHOCK/UNAVAILABLE 禁止开仓。

默认指标为 BOLL(20,2)、RSI(14,70/30)、EMA(9,20)、MACD(5,10,3)、ADX(14) 和 ATR(14)，全部基于已收盘1分钟K线。

趋势信号额外要求方向动能：Call 的 RSI 位于60–70、Put位于30–40，MACD方向一致且柱绝对值至少为 `0.1×ATR(1m)`；价格距离VWAP不得超过 `3×ATR(1m)`。这些门槛用于过滤1分钟假突破，不引入5分钟确认。

固定风控：

- Paper 初始权益 10,000 美元；Live 从 Longbridge 获取实时权益。
- 单笔权利金不超过权益 5%，最多 10 张，每日最多开仓 5 次，平仓后冷却 5 分钟。
- 期权价格下跌 25% 止损；QQQ 结构止损含 `0.1×ATR` 缓冲，距离超过 `2×ATR` 拒绝入场。
- 日内权益亏损达到开盘权益 2% 后清仓并停机。
- +100% 减半并移止损至成本，+250% 清仓；最高浮盈回吐 30% 时全部止盈。
- 持仓满 30 分钟仍亏损则退出。
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

## 本地运行

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

Docker：

```powershell
docker compose up --build
```

Live 模式除 Longbridge 凭证外，还必须设置：

```dotenv
TRADING_MODE=live
ACCOUNT_ID=你的账户ID
LIVE_TRADING_ACK=I_UNDERSTAND_LIVE_TRADING:你的账户ID
```

## 配置边界

在线配置页和回测自定义参数只开放技术指标及 VIX 字段。交易时段、仓位、止盈止损、流动性、手续费和执行规则固定在 `STRATEGY.md`，旧配置版本中的 R 风险、百分比追价、固定行权价偏移等字段会被静默忽略。

基础设施环境变量（账户凭证、数据库、目录、Longbridge、API、日志和调度）仍由 `.env` 管理。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\ruff.exe check src tests
Set-Location frontend
npm run build
```

主要测试文件：

- `tests/test_strategy.py`：指标、OR、完整1分钟K线、即时信号和状态分类。
- `tests/test_risk.py`：选约、流动性、仓位、日亏损和所有固定退出规则。
- `tests/test_backtest.py`：合成报价、聚合退出、13:55 强平和取消回测。
- `tests/test_volatility.py`：VIX 五种状态及方向许可。
- `tests/test_execution_adapter.py`：订单追价、成交与适配器行为。
