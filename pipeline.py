#!/usr/bin/env python3
"""
일일 파이프라인 오케스트레이터

순서:
  1) scrape   : changes.tarkov-changes.com/latest 에서 최신 변경 1건 수집
  2) patchnotes: 최근 공식 패치노트 후보 목록 확보
  3) interpret : LLM 으로 한글 해석 + 패치노트 매칭(잠수함 패치 판별)
  4) store     : data/entries.json 에 신규 항목만 추가(중복 entry_id 스킵)
  5) build     : docs/data.json (사이트가 읽는 피드) 재생성

GitHub Actions 가 매일 이 스크립트를 실행하고, 변경분을 커밋한다.

옵션:
  --force         : 이미 저장된 entry_id 여도 다시 해석해 갱신
  --from-file F   : 라이브 스크래핑 대신 raw JSON 파일에서 입력(테스트/시드용)
  --limit-list N  : (확장용) 추후 /list 다건 처리 대비 자리표시자
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from scripts import scrape as scraper  # noqa: E402
from scripts import patchnotes as pn  # noqa: E402
from scripts import interpret as interp  # noqa: E402

DATA_DIR = ROOT / "data"
ENTRIES_PATH = DATA_DIR / "entries.json"
DOCS_DIR = ROOT / "docs"
FEED_PATH = DOCS_DIR / "data.json"
FEED_LIMIT = 200  # 사이트에 노출할 최대 항목 수


def load_entries() -> list[dict]:
    if ENTRIES_PATH.exists():
        return json.loads(ENTRIES_PATH.read_text(encoding="utf-8"))
    return []


def save_entries(entries: list[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ENTRIES_PATH.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def build_feed(entries: list[dict]) -> None:
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    # 최신순 정렬(scraped_at 기준, 없으면 그대로)
    ordered = sorted(
        entries, key=lambda e: e.get("scraped_at") or "", reverse=True
    )[:FEED_LIMIT]
    feed = {
        "generated_at": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "count": len(ordered),
        "entries": ordered,
    }
    FEED_PATH.write_text(
        json.dumps(feed, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[build] docs/data.json 작성: {len(ordered)}건")


def run(force: bool = False, from_file: str | None = None) -> int:
    # 1) scrape
    if from_file:
        raw = json.loads(Path(from_file).read_text(encoding="utf-8"))
        print(f"[scrape] 파일에서 입력: {from_file}")
    else:
        raw = scraper.scrape()
        print(f"[scrape] entry_id={raw.get('entry_id')} ver={raw.get('eft_version')}")

    entries = load_entries()
    existing_ids = {e.get("entry_id") for e in entries}

    if raw.get("entry_id") in existing_ids and not force:
        print("[skip] 이미 처리된 변경입니다. (신규 없음)")
        build_feed(entries)  # 피드는 항상 최신 상태로 유지
        return 0

    # 2) patchnotes
    notes = pn.get_patch_notes()
    print(f"[patchnotes] 후보 {len(notes)}건")

    # 3) interpret
    processed = interp.interpret(raw, notes)
    flag = "잠수함패치" if processed.get("is_submarine") else "공지연결됨"
    print(f"[interpret] {flag} / {processed.get('summary_ko','')[:40]}")

    # 4) store (신규 추가 또는 force 갱신)
    entries = [e for e in entries if e.get("entry_id") != processed.get("entry_id")]
    entries.append(processed)
    save_entries(entries)

    # 5) build
    build_feed(entries)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--from-file")
    args = ap.parse_args()
    return run(force=args.force, from_file=args.from_file)


if __name__ == "__main__":
    raise SystemExit(main())
