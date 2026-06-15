#!/usr/bin/env python3
"""
과거 이력 수확기 (backfill harvester)

changes.tarkov-changes.com/view/{id} 를 ID 내림차순으로 훑어
실제 과거 사일런트 변경의 raw 데이터를 모아 로컬 파일로 저장한다.
(해석/큐레이션은 이 단계에서 하지 않는다 — 원본만 모은다.)

- /view 페이지에는 자기 자신 링크가 없어 parser 가 entry_id 를 해시로 채우므로,
  여기서 entry_id 를 실제 view id(문자열)로 덮어쓴다.
- 사이트에 부담을 주지 않도록 요청 사이에 지연(--delay)을 둔다.
- 404/파싱 실패는 건너뛰고 계속 진행한다.

사용:
  python scripts/backfill_harvest.py --start 1094 --count 120 --delay 0.7
  python scripts/backfill_harvest.py --start 1094 --stop 1000
출력:
  data/history_raw.json   (raw 항목 배열, id 내림차순)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts import scrape  # noqa: E402

BASE = "https://changes.tarkov-changes.com/view/{id}"
OUT = ROOT / "data" / "history_raw.json"


def harvest(start: int, stop: int, delay: float) -> list[dict]:
    out: list[dict] = []
    for vid in range(start, stop - 1, -1):
        url = BASE.format(id=vid)
        try:
            raw = scrape.scrape(url)
            # /view 에는 self-link 가 없어 entry_id 가 해시로 잡힌다 → 실제 id 로 고정
            raw["entry_id"] = str(vid)
            raw["source_url"] = url
            # 내용이 비면(존재하지 않는 id 등) 건너뜀
            if not raw.get("raw_text") or "Files Changed" not in raw["raw_text"]:
                print(f"[skip] {vid}: 변경 본문 없음")
            else:
                fc = raw.get("files_changed") or []
                tot = sum(f.get("count", 0) for f in fc)
                print(f"[ok]   {vid}: ver={raw.get('eft_version')} files={len(fc)} changes={tot} @ {raw.get('posted_at')}")
                out.append(raw)
        except Exception as e:  # noqa: BLE001
            print(f"[err]  {vid}: {type(e).__name__}: {e}")
        time.sleep(delay)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=int, required=True, help="시작 view id (가장 큰/최신)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--count", type=int, help="이 개수만큼 id 를 내려가며 수확")
    g.add_argument("--stop", type=int, help="이 id 까지(포함) 내려가며 수확")
    ap.add_argument("--delay", type=float, default=0.7, help="요청 간 지연(초)")
    args = ap.parse_args()

    stop = args.stop if args.stop is not None else args.start - args.count + 1
    print(f"[harvest] {args.start} -> {stop} (delay={args.delay}s)")
    rows = harvest(args.start, stop, args.delay)
    OUT.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[done] {len(rows)}건 저장 -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
