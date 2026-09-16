"""List reason codes used in code/DB that lack Chinese labels."""
from __future__ import annotations

import re
from pathlib import Path

from qqq_trader.domain import ExitReason
from qqq_trader.labels import ENTRY_REASON_LABELS, EXIT_REASON_LABELS, REJECT_LABELS

ROOT = Path(__file__).resolve().parents[1] / "src" / "qqq_trader"


def _collect() -> set[str]:
    text = "".join(p.read_text(encoding="utf-8") for p in ROOT.rglob("*.py"))
    found: set[str] = set()
    found |= set(re.findall(r'reject\("([^"]+)"', text))
    found |= set(re.findall(r'record_signal\([^)]+\s"([^"]+)"', text))
    found |= set(re.findall(r'signal\(signal, False, "([^"]+)"', text))
    found |= set(re.findall(r'reason=f"entry_\{signal\.strategy\}"', text))  # noop marker
    found |= {e.value for e in ExitReason}
    strat = Path(ROOT / "strategy.py").read_text(encoding="utf-8")
    for s in re.findall(r'_signal\(Direction\.[A-Z]+,\s*"([^"]+)"', strat):
        found.add(s)
        found.add(f"entry_{s}")
    # Known extras from backtest / persistence / engine
    found |= {
        "daily_loss",
        "accepted",
        "recovered",
        "macd_reversal_pending_volume_confirmation",
        "macd_reversal_pending_cancelled",
        "pyramid_add_1",
        "pyramid_add_2",
        "entry_abandoned",
        "entry_exception",
        "missing_option_frame",
        "shutdown",
    }
    return found


_SKIP = {"accepted", "executed", "rejected", 'reason=f"entry_{signal.strategy}"'}


def _covered(key: str) -> bool:
    if key in _SKIP:
        return True
    if key in ENTRY_REASON_LABELS or key in EXIT_REASON_LABELS or key in REJECT_LABELS:
        return True
    if key.startswith("entry_") and key.removeprefix("entry_") in ENTRY_REASON_LABELS:
        return True
    if key.startswith("option_chain_error"):
        return "option_chain_error" in REJECT_LABELS
    if key.startswith("volatility_"):
        if key in REJECT_LABELS:
            return True
        for rk in REJECT_LABELS:
            if key.startswith(rk):
                return True
    return False


def main() -> None:
    missing = sorted(k for k in _collect() if not _covered(k))
    print(f"Checked {len(_collect())} reason tokens; missing labels: {len(missing)}")
    for m in missing:
        print(f"  {m}")


if __name__ == "__main__":
    main()
