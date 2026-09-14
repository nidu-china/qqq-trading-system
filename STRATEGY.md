# QQQ 0DTE 交易策略

系统只运行 **Hybrid**（制度自适应）。Paper、Live 和 Replay 共用同一套
入场、VIX 过滤、选约和风控。所有计算只用已收盘的 1 分钟 RTH K 线，时间为美东（ET）。

Jul–Sep 2026 对比后下线了独立的 Trend ORB 与 BOLL/MACD 模式。

---

## Hybrid

### 时间状态机

| ET 时间 | 行为 |
|---|---|
| 09:30–09:40 | 收集 Opening Range，不开仓 |
| 09:40–13:30 | 分类制度并允许入场 |
| 12:00 后 | 新信号评分门槛 ≥ 7 |
| 13:30–13:55 | 不再开新仓，管理已有仓位 |
| 13:55 | 强制清仓 |

### 制度

连续 3 根 K 线投票确认后切换：`TREND_UP` / `TREND_DOWN` / `RANGE` / `UNKNOWN`。
趋势制度走 OR 突破与 VWAP 回撤；震荡/未知走超卖反弹、VWAP 结构、OR 回归，最后兜底假突破陷阱。

`regime_momentum_3bar` 已关闭。

### 风控（与引擎共用）

- 权利金跌 25% 止损；止损后冷却 30 分钟；同向 2 次止损封锁当日该方向。
- 每日最多 5 笔；普通平仓冷却 3 分钟。
- +100% 减半，+250% 清仓；最高浮盈 ≥25% 后回吐 30% 触发移动止盈。
- 持仓满 20 分钟仍亏则退出。
- 不设每日亏损熔断（`max_daily_loss_pct=0`）。

## 合约、流动性和 VIX

0DTE，行权价在现货 ±$1 内取最近 5 张，再选合格报价里 Ask 最低的。
VIX：NORMAL / 数据不可用双向；RISK_OFF 仅 Put；RECOVERY 仅 Call；SHOCK 禁止开仓。

## 回测

- 优先历史期权 Ask 入场、Bid 退出。
- 缺报价时用 0DTE Black-Scholes 逐分钟重定价；IV 只用来自决策时点之前的数据。

```powershell
qqq-trader backtest --bars <QQQ数据路径>
```
