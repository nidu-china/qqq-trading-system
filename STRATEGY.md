# QQQ 0DTE 交易策略

系统只运行 **Hybrid**（制度自适应）。Paper、Live、Replay 共用同一套策略引擎、选约与风控。

Jul–Sep 2026 对比后已下线独立的 Trend ORB、纯 BOLL/MACD 与「VIX 走势模式」子策略；逻辑合并进 Hybrid 的信号链与 VIX 方向闸门。

---

## 数据与指标

- **指标会话**：仅使用**当天** 09:00–16:00 ET 已收盘的 1 分钟 QQQ K 线（**不含**前一交易日）。换日清空 BOLL/MACD/EMA/RSI、挤压 armed 等日内状态。
- **交易 K 线**：Regular 09:30–16:00；Opening Range 在 09:30–`phase_collect_end` 内累计。
- **默认参数**（`StrategyRules`）：BOLL(20,2)、EMA(21/… 快慢由 trend 参数)、标准 MACD(8,17,9)、快 MACD(5,10,3) 用于部分入场/陷阱；RSI(14)。09:00 起约 20–25 根后指标可用。
- **VIX**：不参与产生 Call/Put，仅作**成交前**方向过滤（见下文）。VIX 1m MACD 使用**当天** 04:00–16:00 ET 的 1 分钟 K 线（与 Hybrid 相同的 8/17/9）。

---

## 时间状态机

| ET 时间 | 行为 |
|--------|------|
| 09:00–09:30 | 指标预热（含挤压带宽），**不开仓** |
| 09:30–09:35 | 收集 Opening Range（OR 高/低），**不开仓** |
| 09:35–10:00 | Phase 2：制度未确认时允许 OR 突破类信号；趋势制度内可走 OR 突破子逻辑 |
| 09:35–13:30 | 主窗口：按制度走信号链（`phase_main_end`） |
| ≥12:00 | 信号评分门槛从 4 提高到 **≥7**（午后 0DTE 衰减） |
| ≥13:30 | **不再开新仓**，仅管理持仓 |
| 13:55 | **强制清仓**（`forced_close`） |

引擎另限制：信号须在 `phase_collect_end`–`phase_main_end` 内，且信号年龄 ≤ `signal_ttl_seconds`（默认 90s）。

---

## 制度（Regime）

- 每根 RTH K 线用 `_raw_regime()` 得到瞬时状态，**连续 3 根同向**后切换为确认制度：`TREND_UP` / `TREND_DOWN` / `RANGE` / `UNKNOWN`（收集阶段为 `OBSERVATION`）。
- **趋势日**（`TREND_UP` / `TREND_DOWN`）：优先趋势跟随与 VWAP 结构空单。
- **震荡**（`RANGE`）与 **未知**（`UNKNOWN`）：均值回归链 + 挤压突破 + OR 回归；UNKNOWN 在 10:00 前额外允许 Phase2 OR 突破。
- **`regime_momentum_3bar`**、**`_range_signal`（外轨回归）**、**`regime_momentum_3bar`** 等已从 evaluate 链移除或恒为 `return None`。

---

## 信号链（按 evaluate 优先级）

同一根 K 只取链上**第一个**非空信号；各子策略另有独立门槛（RSI、量比、VWAP、MACD、OR、挤压等）。

### A. `TREND_UP` / `TREND_DOWN`

| 顺序 | 策略 ID | 方向 | 要点 |
|-----|---------|------|------|
| 1 | `vwap_reclaim_call` | Call | 仅 **TREND_DOWN** 时尝试：连续 2 根收在 VWAP 下后，当根**收复 VWAP**（阳线、RSI≤45、BandPos≤0.2） |
| 2 | `regime_trend_or_breakout` | Call/Put | **09:35–10:00**：收盘突破 OR 高/低 + MACD 方向 + RSI 带 + 量比 |
| 2 | `regime_trend_following` | Call/Put | **10:00+**：已突破 OR + BandPos ≥±0.65 + MACD 符号/斜率 + RSI 带 + 量比；过 chop / 日内低方向性则跳过 |
| 3 | `vwap_pullback` | Put（主） | EMA 空排 + 前一根触 VWAP 区 + 收 VWAP 下 + 阴线 + 快 MACD 负且 2  bar 下行；**≥09:44**；同函数内 **Call** 分支为 `vwap_bounce_call`（见下） |

趋势链中 **`vwap_macd_fade` 已注释关闭**（回测拖累）。

### B. `RANGE`

| 顺序 | 策略 ID | 方向 | 要点 |
|-----|---------|------|------|
| 1 | `squeeze_mid_break` | Call | 见「挤压中轨突破」 |
| 2 | `deep_oversold_bounce` | Call | RSI≤38、BandPos≤-0.60、价在 VWAP 下、非强空制度等 |
| 3 | `vwap_reclaim_call` | Call | 收复 VWAP（见上） |
| 4 | `vwap_pullback` / `vwap_bounce_call` | Put / Call | Put：VWAP 拒绝；Call：VWAP 下超卖 + MACD 刚拐头 + 阳线，**≥10:00** |
| 5 | `macd_narrowing_call` | Call | MACD 柱收窄/拐头类做多（与 bounce 互补、偏更早） |
| 6 | `regime_or_reversion` | Call/Put | 价在 OR 外：低于 OR 低做多 / 高于 OR 高做空；**评分 ≥6**（噪声高） |

**已关闭**：`vwap_macd_fade`、`momentum_exhaustion_put`、`macd_narrowing_put`、`_momentum_signal`（3-bar）。

### C. `UNKNOWN`

| 顺序 | 策略 ID | 说明 |
|-----|---------|------|
| 1 | `regime_or_breakout` | **仅 09:35–10:00**：OR 突破 + 快 MACD + chop 过滤 |
| 2–6 | 同 RANGE | `squeeze_mid_break` → … → `regime_or_reversion`（顺序略同，无 trend 专用项） |

### D. 全制度兜底

| 策略 ID | 方向 | 要点 |
|---------|------|------|
| `trap_false_breakout` | Put | 假突破 OR 高后回落 |
| `trap_false_breakdown` | Call | 假跌破 OR 低后收回 |

陷阱与部分回归信号在 **`squeeze_armed`** 期间不抢 slot。

---

## 挤压中轨突破（`squeeze_mid_break`）

仅 **RANGE / UNKNOWN**，**Call**。

1. **挤压**：带宽在 90 根 lookback 内处于 **≤25 分位**，且连续 **≥5 根** 满足挤压。
2. **Armed**：线圈结束后 **20 根**内仍有效（线圈可已开始扩张）。
3. **绝对带宽上限**：整段线圈 **min 带宽 ≤ 0.29%**（`squeeze_max_coil_width`）。
4. **开火**：带宽相对线圈尾部 **≥1.05×** 扩张 + **收盘上穿 BOLL 中轨** + **BandPos ≤ 0.70**。
5. 每个线圈 **最多开火一次**；开火后标记 reset，armed 失效再重新计。

**出场**：收 **跌破 BOLL 中轨** → `bollinger_middle`（bar 级，优先于期权移动止盈）。

---

## 信号评分（`signal_score`）

- 默认最低分 **4**；`regime_or_reversion` 最低 **6**；**12:00 后** 最低 **7**。
- 下列策略**豁免**评分（自有硬门槛）：`regime_trend_following`、`regime_trend_or_breakout`、`regime_or_breakout`、`vwap_pullback`、`vwap_bounce_call`、`vwap_reclaim_call`、`squeeze_mid_break`。
- 同方向 **2 次止损** → 当日封锁该方向；评分通过仍会被 `_direction_blocked` 丢弃。

---

## 持仓出场（Hybrid bar + 共用 RiskEngine）

**Bar 级（`bar_exit_decision`）**

| 策略类型 | 典型 bar 出场 |
|---------|----------------|
| `squeeze_mid_break` | 收 < BOLL 中轨 |
| `regime_or_reversion` 等均值回归 | 触及 BOLL 中轨；或 MACD 反转 + 量确认 → `direction_reversal` |
| `vwap_reclaim_call` | 收 < VWAP 或 < 慢 EMA |
| 趋势 / `vwap_pullback` 等 | 制度反向或 EMA 破坏 → `state_invalidation` / `trend_ema_exit`；盈利 ≥0.5 ATR 后 **1 ATR 拖尾** → `trailing_stop` |

**期权级（RiskEngine，全策略）**

- 权利金 **-25%** 止损；**+100%** 减半、**+250%** 清仓；浮盈 **≥25%** 后回吐 **30%** 移动止盈。
- 持仓 **20 分钟**仍亏 → `stale_position`。
- **13:55** `forced_close`；可选日亏熔断（默认 `max_daily_loss_pct=0` 关闭）。

---

## 引擎风控与 VIX 闸门

- 每日最多 **5** 笔；普通平仓冷却 **3** 分钟；止损后冷却 **30** 分钟。
- 0DTE：行权价现货 **±$1** 内最近 5 张 → 流动性过滤 → Ask 最低。
- Hybrid 先产生 Call/Put，再经 VIX 1m MACD 柱过滤：
  - **柱 > 0**（`vix_macd_rising`）：默认 **拒绝 Call**、允许 Put；`.env` 中 **`VOLATILITY_VIX_MACD_RISING_BLOCK=false`** 时 rising 仅标记、**不拦 Call**。
  - **柱 < 0**（`vix_macd_falling`）：**拒绝 Put**、允许 Call。
  - **柱 = 0** 或 VIX **不可用**：双向放行（live 会打日志）。

---

## 代码中保留但未入链的策略

下列函数仍在代码库中，**当前 evaluate 不调用或恒返回 None**，仅供试验/回滚：

- `vwap_macd_fade`、`momentum_exhaustion_put`、`macd_narrowing_put`
- `_momentum_signal`（3-bar）
- `_range_signal`（`regime_range_reversion` 外轨）

---

## 回测

- 优先历史期权 **Ask 入 / Bid 出**。
- 缺报价时用 0DTE Black-Scholes 逐分钟重定价；IV 仅使用决策时点之前的数据。
- 回测指标会话与 live 一致：**当日 09:00 起**累积，不跨日污染 BOLL/挤压。

```powershell
qqq-trader backtest --bars <QQQ数据路径>
```

界面「原因」列中文与策略 ID 对照见 `src/qqq_trader/labels.py`（`entry_*` / 拒单 / 出场）。
