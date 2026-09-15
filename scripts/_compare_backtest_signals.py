"""Compare hybrid trade entries between two backtest report files."""
from __future__ import annotations

import re
from pathlib import Path

OLD = Path("reports/backtest_3strategy_jul_aug_sep.txt")
NEW = Path("reports/backtest_hybrid_jul_aug_sep.txt")

ROW = re.compile(
    r"^\s*\d+\s+(\d{2}/\d{2}\s+\d{2}:\d{2})\s+.*?\s+(PUT|CALL)\s+(\S+)\s+\d+"
)


def parse(path: Path) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = ROW.match(line)
        if m:
            rows.append((m.group(1), m.group(2), m.group(3)))
    return rows


def key(r: tuple[str, str, str]) -> tuple[str, str, str]:
    return r


def main() -> None:
    old = parse(OLD)
    new = parse(NEW)
    old_set = {key(r) for r in old}
    new_set = {key(r) for r in new}
    both = sorted(old_set & new_set)
    only_old = sorted(old_set - new_set)
    only_new = sorted(new_set - old_set)

    print(f"OLD trades: {len(old)}  unique keys: {len(old_set)}")
    print(f"NEW trades: {len(new)}  unique keys: {len(new_set)}")
    print(f"Exact match (time+dir+strategy): {len(both)}")
    print()

    # Match ignoring strategy (same minute + dir)
    def minute_dir(r):
        return (r[0], r[1])

    old_md = {minute_dir(r): r for r in old}
    new_md = {minute_dir(r): r for r in new}
    md_both = sorted(set(old_md) & set(new_md))
    same_dir_time = []
    diff_strategy = []
    for md in md_both:
        if old_md[md][2] == new_md[md][2]:
            same_dir_time.append(md)
        else:
            diff_strategy.append((md, old_md[md][2], new_md[md][2]))
    print(f"Same entry minute + direction: {len(md_both)}")
    print(f"  same strategy too: {len(same_dir_time)}")
    print(f"  different strategy at same minute: {len(diff_strategy)}")
    print()

    print("=== ONLY IN OLD (first 40) ===")
    for r in only_old[:40]:
        print(f"  {r[0]}  {r[1]:4}  {r[2]}")
    if len(only_old) > 40:
        print(f"  ... +{len(only_old) - 40} more")
    print()

    print("=== ONLY IN NEW (first 40) ===")
    for r in only_new[:40]:
        print(f"  {r[0]}  {r[1]:4}  {r[2]}")
    if len(only_new) > 40:
        print(f"  ... +{len(only_new) - 40} more")
    print()

    # July-only breakdown
    def is_jul(r):
        return r[0].startswith("07/")

    old_jul = [r for r in old if is_jul(r)]
    new_jul = [r for r in new if is_jul(r)]
    oj = {key(r) for r in old_jul}
    nj = {key(r) for r in new_jul}
    print(f"JULY: old {len(old_jul)} trades, new {len(new_jul)}")
    print(f"  exact match: {len(oj & nj)}")
    print(f"  only old: {len(oj - nj)}")
    print(f"  only new: {len(nj - oj)}")

    # Big old winners missing in new
    big_old = [
        "07/27 10:12",
        "07/31 10:04",
        "07/23 10:36",
        "07/29 09:48",
        "08/19 09:48",
    ]
    print()
    print("=== Spot-check big OLD entries in NEW? ===")
    new_times = {r[0] for r in new}
    for t in big_old:
        strat_old = [r for r in old if r[0] == t]
        in_new = [r for r in new if r[0] == t]
        print(f"  {t}  old={strat_old}  new={in_new or 'MISSING'}")


if __name__ == "__main__":
    main()
