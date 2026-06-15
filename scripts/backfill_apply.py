#!/usr/bin/env python3
"""
백필 적용기

data/backfill_interp.json (사람이 작성한 한글 해석) 을
data/history_raw.json (실제 과거 raw 데이터) 와 결합해
완전한 entry 로 만들어 data/entries.json 에 병합하고 docs/data.json 을 재생성한다.

- raw 패스스루(eft_version/posted_at/files_changed/raw_text/source_url)는 history_raw 에서 가져온다.
- entry 에 backfilled=true 표시, is_submarine 은 patch_note.matched 로 결정.
- 기존 entries.json 의 가짜 시드(REMOVE_IDS)는 제거한다.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pipeline  # noqa: E402  (load_entries/save_entries/build_feed 재사용)

INTERP = ROOT / "data" / "backfill_interp.json"
HISTORY = ROOT / "data" / "history_raw.json"

# 실제 과거 데이터가 아닌 예시 시드. 실제로는 view/1094 가 동일 내용의 진짜 항목.
REMOVE_IDS = {"1084"}


def main() -> int:
    interp = json.loads(INTERP.read_text(encoding="utf-8"))
    history = {r["entry_id"]: r for r in json.loads(HISTORY.read_text(encoding="utf-8"))}

    built: list[dict] = []
    for eid, ov in interp.items():
        if eid.startswith("_"):
            continue
        raw = history.get(eid)
        if not raw:
            print(f"[warn] history_raw 에 id={eid} 없음 — 건너뜀")
            continue
        pn = ov.get("patch_note") or {}
        entry = {
            "entry_id": eid,
            "eft_version": raw.get("eft_version"),
            "posted_at": raw.get("posted_at"),
            "scraped_at": ov.get("scraped_at") or raw.get("scraped_at"),
            "source_url": raw.get("source_url"),
            "files_changed": raw.get("files_changed", []),
            "summary_ko": ov.get("summary_ko"),
            "tags": ov.get("tags", []),
            "severity": ov.get("severity", "minor"),
            "changes": ov.get("changes", []),
            "patch_note": pn,
            "is_submarine": not bool(pn.get("matched")),
            "backfilled": True,
            "recurring_event": bool(ov.get("recurring_event")),
            "raw_text": raw.get("raw_text", ""),
        }
        built.append(entry)

    entries = pipeline.load_entries()
    keep = [e for e in entries if e.get("entry_id") not in REMOVE_IDS]
    removed = len(entries) - len(keep)

    by_id = {e.get("entry_id"): e for e in keep}
    for e in built:
        by_id[e["entry_id"]] = e  # 신규/갱신
    merged = list(by_id.values())

    pipeline.save_entries(merged)
    pipeline.build_feed(merged)
    print(f"[backfill] 시드제거 {removed}건 / 백필 {len(built)}건 / 총 {len(merged)}건")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
