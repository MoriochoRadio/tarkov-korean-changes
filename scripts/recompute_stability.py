#!/usr/bin/env python3
"""
안정성 재계산기

기존 data/entries.json 전체에 대해 안정성(stable/superseded/recurring)을
다시 판정해 부여하고 docs/data.json 을 재생성한다.
파서/판정 로직을 바꿨거나 이력이 늘었을 때 한 번 돌리면 된다.

  python scripts/recompute_stability.py
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pipeline  # noqa: E402


def main() -> int:
    entries = pipeline.load_entries()
    pipeline.finalize(entries)
    dist = Counter(e.get("stability") for e in entries)
    print(f"[recompute] {len(entries)}건 재판정 → {dict(dist)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
