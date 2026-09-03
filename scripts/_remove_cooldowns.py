"""One-shot script: remove all non-trap cooldown logic from hybrid_strategy.py."""
import re
from pathlib import Path

src = Path("src/qqq_trader/hybrid_strategy.py").read_text(encoding="utf-8")

attrs_to_remove = [
    "vwap_bounce_bar", "vwap_fade_bar", "momentum_exhaustion_bar",
    "macd_narrowing_call_bar", "macd_narrowing_put_bar",
    "or_reversion_bar", "vwap_pullback_bar",
]

# 1. Remove __init__ cooldown declarations (comment + attribute line)
for attr in attrs_to_remove:
    src = re.sub(
        r"\n        # [^\n]+\n        self\._last_" + attr + r"[^\n]+\n",
        "\n",
        src,
    )

# 2. Remove _reset_day assignments
for attr in attrs_to_remove:
    src = re.sub(r"\n        self\._last_" + attr + r" = None\n", "\n", src)

# 3. Remove cooldown check+set blocks for each signal method
# Each block has the pattern:
#   [comment]\n  if self._last_XXX_bar...\n    bars_since = ...\n    if bars_since < N:\n      return None\n  sig = self._signal(...)\n  self._last_XXX_bar = sig.bar_end\n  return sig\n
# → Replace with just:  return self._signal(...)\n

replacements = [
    # vwap_bounce_call
    (
        r"            # Cooldown: 1[05]-bar minimum between fires to avoid repeated entries in same zone\n"
        r"            if self\._last_vwap_bounce_bar is not None and ctx\.bar_end is not None:\n"
        r"                bars_since = sum\(1 for b in self\._today_bars if b\.end > self\._last_vwap_bounce_bar\)\n"
        r"                if bars_since < 1[05]:\n"
        r"                    return None\n"
        r"            sig = self\._signal\(Direction\.CALL, \"vwap_bounce_call\", spot\)\n"
        r"            self\._last_vwap_bounce_bar = sig\.bar_end\n"
        r"            return sig\n",
        '            return self._signal(Direction.CALL, "vwap_bounce_call", spot)\n',
    ),
    # vwap_pullback PUT
    (
        r"            # Cooldown: 1[05]-bar minimum between VWAP rejection entries\n"
        r"            if self\._last_vwap_pullback_bar is not None and ctx\.bar_end is not None:\n"
        r"                bars_since = sum\(1 for b in self\._today_bars if b\.end > self\._last_vwap_pullback_bar\)\n"
        r"                if bars_since < 10:\n"
        r"                    return None\n"
        r"            sig = self\._signal\(Direction\.PUT, \"vwap_pullback\", spot\)\n"
        r"            self\._last_vwap_pullback_bar = sig\.bar_end\n"
        r"            return sig\n",
        '            return self._signal(Direction.PUT, "vwap_pullback", spot)\n',
    ),
    # vwap_macd_fade
    (
        r"            # Cooldown: 1[05]-bar minimum between fires to avoid repeated exhaustion entries\n"
        r"            if self\._last_vwap_fade_bar is not None and ctx\.bar_end is not None:\n"
        r"                bars_since = sum\(1 for b in self\._today_bars if b\.end > self\._last_vwap_fade_bar\)\n"
        r"                if bars_since < 15:\n"
        r"                    return None\n"
        r"            sig = self\._signal\(Direction\.PUT, \"vwap_macd_fade\", spot\)\n"
        r"            self\._last_vwap_fade_bar = sig\.bar_end\n"
        r"            return sig\n",
        '            return self._signal(Direction.PUT, "vwap_macd_fade", spot)\n',
    ),
    # momentum_exhaustion_put
    (
        r"            # Independent 12-bar cooldown \(does not share with vwap_macd_fade\)\n"
        r"            if self\._last_momentum_exhaustion_bar is not None and ctx\.bar_end is not None:\n"
        r"                bars_since = sum\(1 for b in self\._today_bars if b\.end > self\._last_momentum_exhaustion_bar\)\n"
        r"                if bars_since < 12:\n"
        r"                    return None\n"
        r"            sig = self\._signal\(Direction\.PUT, \"momentum_exhaustion_put\", spot\)\n"
        r"            self\._last_momentum_exhaustion_bar = sig\.bar_end\n"
        r"            return sig\n",
        '            return self._signal(Direction.PUT, "momentum_exhaustion_put", spot)\n',
    ),
    # deep_oversold_bounce
    (
        r"            # Shared cooldown with vwap_bounce_call \(both target oversold bounces\)\n"
        r"            if self\._last_vwap_bounce_bar is not None and ctx\.bar_end is not None:\n"
        r"                bars_since = sum\(1 for b in self\._today_bars if b\.end > self\._last_vwap_bounce_bar\)\n"
        r"                if bars_since < 15:\n"
        r"                    return None\n"
        r"            sig = self\._signal\(Direction\.CALL, \"deep_oversold_bounce\", spot\)\n"
        r"            self\._last_vwap_bounce_bar = sig\.bar_end\n"
        r"            return sig\n",
        '            return self._signal(Direction.CALL, "deep_oversold_bounce", spot)\n',
    ),
    # macd_narrowing_call
    (
        r"            # Independent 12-bar cooldown\n"
        r"            if self\._last_macd_narrowing_call_bar is not None and ctx\.bar_end is not None:\n"
        r"                bars_since = sum\(1 for b in self\._today_bars if b\.end > self\._last_macd_narrowing_call_bar\)\n"
        r"                if bars_since < 12:\n"
        r"                    return None\n"
        r"            sig = self\._signal\(Direction\.CALL, \"macd_narrowing_call\", spot\)\n"
        r"            self\._last_macd_narrowing_call_bar = sig\.bar_end\n"
        r"            return sig\n",
        '            return self._signal(Direction.CALL, "macd_narrowing_call", spot)\n',
    ),
    # macd_narrowing_put
    (
        r"            # Independent 12-bar cooldown\n"
        r"            if self\._last_macd_narrowing_put_bar is not None and ctx\.bar_end is not None:\n"
        r"                bars_since = sum\(1 for b in self\._today_bars if b\.end > self\._last_macd_narrowing_put_bar\)\n"
        r"                if bars_since < 12:\n"
        r"                    return None\n"
        r"            sig = self\._signal\(Direction\.PUT, \"macd_narrowing_put\", spot\)\n"
        r"            self\._last_macd_narrowing_put_bar = sig\.bar_end\n"
        r"            return sig\n",
        '            return self._signal(Direction.PUT, "macd_narrowing_put", spot)\n',
    ),
    # or_reversion CALL cooldown
    (
        r"            # Cooldown: 10-bar minimum between OR reversion fires in same direction\n"
        r"            if self\._last_or_reversion_bar is not None and ctx\.bar_end is not None:\n"
        r"                bars_since = sum\(1 for b in self\._today_bars if b\.end > self\._last_or_reversion_bar\)\n"
        r"                if bars_since < 10:\n"
        r"                    return None\n"
        r"            sig = self\._signal\(Direction\.CALL, \"regime_or_reversion\", spot\)\n"
        r"            self\._last_or_reversion_bar = sig\.bar_end\n"
        r"            return sig\n",
        '            return self._signal(Direction.CALL, "regime_or_reversion", spot)\n',
    ),
    # or_reversion PUT cooldown
    (
        r"            # Cooldown: 10-bar minimum between OR reversion fires in same direction\n"
        r"            if self\._last_or_reversion_bar is not None and ctx\.bar_end is not None:\n"
        r"                bars_since = sum\(1 for b in self\._today_bars if b\.end > self\._last_or_reversion_bar\)\n"
        r"                if bars_since < 10:\n"
        r"                    return None\n"
        r"            sig = self\._signal\(Direction\.PUT, \"regime_or_reversion\", spot\)\n"
        r"            self\._last_or_reversion_bar = sig\.bar_end\n"
        r"            return sig\n",
        '            return self._signal(Direction.PUT, "regime_or_reversion", spot)\n',
    ),
]

for pattern, replacement in replacements:
    new_src = re.sub(pattern, replacement, src)
    if new_src == src:
        print(f"WARNING: no match for pattern starting with: {pattern[:80]!r}")
    else:
        print(f"OK: replaced pattern starting with: {pattern[:60]!r}")
    src = new_src

# Verify all attrs gone from init (not _reset_day)
print("\nRemaining _last_*_bar in init block:")
init_end = src.find("def _reset_day")
init_block = src[src.find("def __init__"):init_end]
for line in init_block.split("\n"):
    if "_last_" in line and "_bar" in line:
        print(" ", line)

print("\nRemaining _last_*_bar in _reset_day block:")
reset_end = src.find("def set_volatility_context")
reset_block = src[init_end:reset_end]
for line in reset_block.split("\n"):
    if "_last_" in line and "_bar" in line:
        print(" ", line)

Path("src/qqq_trader/hybrid_strategy.py").write_text(src, encoding="utf-8")
print("\nSaved.")
