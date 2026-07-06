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
from scripts import stability as stab  # noqa: E402

DATA_DIR = ROOT / "data"
ENTRIES_PATH = DATA_DIR / "entries.json"
HISTORY_PATH = DATA_DIR / "history_raw.json"
FAILS_PATH = DATA_DIR / "scrape_failures.json"  # 스크랩 연속 실패 카운터
DOCS_DIR = ROOT / "docs"
FEED_PATH = DOCS_DIR / "data.json"
FEED_LIMIT = 200  # 사이트에 노출할 최대 항목 수

# 와이프급 패치는 raw_text가 수 MB — 무상한 저장 시 entries.json이 저장소를,
# docs/data.json이 방문자 대역폭(모바일 포함)을 잡아먹는다. 원본 전문은 소스 링크에 있다.
RAW_TEXT_STORE_LIMIT = 100_000  # data/entries.json 보존 상한(stability 파싱용 여유)
RAW_TEXT_FEED_LIMIT = 20_000    # docs/data.json(사이트 전송) 상한
TRUNC_NOTICE = "\n\n…(원문이 매우 커서 잘렸습니다 — 전체는 원본 링크에서 확인)"


def clip_raw(text: str | None, limit: int) -> str:
    text = text or ""
    if len(text) > limit:
        return text[:limit] + TRUNC_NOTICE
    return text


def load_entries() -> list[dict]:
    if ENTRIES_PATH.exists():
        return json.loads(ENTRIES_PATH.read_text(encoding="utf-8"))
    return []


def make_locked_entry(raw: dict) -> dict:
    """접근 제한(로그인 게이트) 상태의 변경을 LLM 호출 없이 '공개 대기' 항목으로 만든다.

    원본 식별 정보는 보존해 두고, 잠금이 풀리면 reprocess_locked 가 /view/{id} 로
    다시 수집해 실제 해석으로 교체한다.
    """
    return {
        "entry_id": raw.get("entry_id"),
        "eft_version": raw.get("eft_version"),
        "posted_at": raw.get("posted_at"),
        "scraped_at": raw.get("scraped_at"),
        "source_url": raw.get("source_url"),
        "files_changed": raw.get("files_changed", []),
        "raw_text": clip_raw(raw.get("raw_text"), RAW_TEXT_STORE_LIMIT),
        "locked": True,
        "summary_ko": "🔒 공개 대기 중 — 원본이 게시 후 12시간 동안 비공개입니다. "
                      "잠금이 풀리면 다음 갱신 때 자동으로 한글 해석이 채워집니다.",
        "tags": ["공개대기"],
        "severity": "trivial",
        "changes": [],
        "patch_note": {
            "matched": False, "title": None, "url": None,
            "reason_ko": "원본 접근 제한으로 아직 분석하지 않았습니다.",
        },
        "is_submarine": True,
    }


def reprocess_locked(entries: list[dict], notes: list[dict]) -> bool:
    """이전에 '공개 대기'로 보류된 항목을 /view/{id} 로 재수집한다.

    잠금이 풀려 실제 변경 본문이 보이면 LLM 해석으로 교체하고, 아직 잠겨 있으면
    그대로 둔다. 정렬이 흔들리지 않도록 원래 scraped_at 은 보존한다.
    반환값: 하나라도 갱신했으면 True.
    """
    changed = False
    for i, e in enumerate(entries):
        if not e.get("locked"):
            continue
        eid = e.get("entry_id")
        if not eid or not str(eid).isdigit():
            print(f"[reprocess] {eid}: view id 가 아니어서 재수집 불가 — 유지")
            continue
        try:
            raw = scraper.scrape_view(eid)
        except Exception as ex:  # noqa: BLE001
            print(f"[reprocess] {eid}: 재수집 실패(유지) — {ex}")
            continue
        rt = raw.get("raw_text", "")
        if scraper.is_locked(rt) or not scraper.has_diff(rt):
            print(f"[reprocess] {eid}: 아직 잠김 — 유지")
            continue
        processed = interp.interpret(raw, notes)
        processed.pop("locked", None)
        processed["scraped_at"] = e.get("scraped_at") or processed.get("scraped_at")
        entries[i] = processed
        changed = True
        print(f"[reprocess] {eid}: 잠금 해제 → 재해석 완료 ({processed.get('summary_ko','')[:36]})")
    return changed


def save_entries(entries: list[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for e in entries:  # 기존 대형 항목도 저장 시점에 함께 정규화된다(멱등)
        e["raw_text"] = clip_raw(e.get("raw_text"), RAW_TEXT_STORE_LIMIT)
    ENTRIES_PATH.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def annotate_stability(entries: list[dict]) -> list[dict]:
    """각 entry 에 stability/stability_detail_ko/recurring_event 를 자동 부여.

    재발(토글) 탐지 정확도를 위해 data/history_raw.json 의 raw 도 인덱스에 합친다
    (중복 entry_id 는 entries 쪽을 우선).
    """
    base = list(entries)
    if HISTORY_PATH.exists():
        try:
            hist = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
            have = {e.get("entry_id") for e in base}
            base += [h for h in hist if h.get("entry_id") not in have]
        except Exception as e:  # noqa: BLE001
            print(f"[stability] history_raw 로드 실패(무시): {e}")
    index = stab.build_index(base)
    toggling = stab.toggling_keys(index)
    for e in entries:
        e.update(stab.assess(e, index, toggling))
    return entries


def finalize(entries: list[dict]) -> None:
    """안정성 주석 → entries.json 저장 → docs/data.json 재생성."""
    annotate_stability(entries)
    save_entries(entries)
    build_feed(entries)


def build_feed(entries: list[dict]) -> None:
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    # 최신순 정렬(scraped_at 기준, 없으면 그대로)
    ordered = sorted(
        entries, key=lambda e: e.get("scraped_at") or "", reverse=True
    )[:FEED_LIMIT]
    # 사이트 피드에는 더 짧은 상한 적용(entries.json 원본은 그대로 둠)
    ordered = [
        {**e, "raw_text": clip_raw(e.get("raw_text"), RAW_TEXT_FEED_LIMIT)}
        for e in ordered
    ]
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


def _scrape_failures(reset: bool = False) -> int:
    """스크랩 연속 실패 일수를 기록/조회한다. 성공 시 reset=True 로 초기화."""
    if reset:
        if FAILS_PATH.exists():
            FAILS_PATH.write_text('{"consecutive": 0}', encoding="utf-8")
        return 0
    count = 0
    if FAILS_PATH.exists():
        try:
            count = int(json.loads(FAILS_PATH.read_text(encoding="utf-8")).get("consecutive", 0))
        except Exception:  # noqa: BLE001
            count = 0
    count += 1
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    FAILS_PATH.write_text(json.dumps({"consecutive": count}), encoding="utf-8")
    return count


def find_twin(entries: list[dict], raw: dict) -> dict | None:
    """같은 변경이 해시 ID와 숫자 ID 로 두 번 수집되는 것을 posted_at+eft_version 으로 탐지."""
    key = (raw.get("posted_at"), raw.get("eft_version"))
    if not all(key):
        return None
    for e in entries:
        if (
            (e.get("posted_at"), e.get("eft_version")) == key
            and e.get("entry_id") != raw.get("entry_id")
        ):
            return e
    return None


def run(force: bool = False, from_file: str | None = None) -> int:
    # 1) scrape
    if from_file:
        raw = json.loads(Path(from_file).read_text(encoding="utf-8"))
        print(f"[scrape] 파일에서 입력: {from_file}")
    else:
        try:
            raw = scraper.scrape()
        except Exception as ex:  # noqa: BLE001
            # 업스트림 장애(수 분 이상 지속되는 502 등) — 기존 데이터로 피드는 재생성하고,
            # 1회성이면 조용히 넘어간다. /latest 는 최신 1건만 보여줘 이틀 연속이면
            # 유실 위험이 실제가 되므로 그때만 런을 실패시켜 알림을 받는다.
            fails = _scrape_failures()
            print(f"[scrape] 실패({fails}일 연속): {ex}")
            entries = load_entries()
            if any(e.get("locked") for e in entries):
                try:
                    reprocess_locked(entries, pn.get_patch_notes())
                except Exception as ex2:  # noqa: BLE001
                    print(f"[reprocess] 보류 항목 재처리 실패(무시): {ex2}")
            finalize(entries)
            return 1 if fails >= 2 else 0
        _scrape_failures(reset=True)
        print(f"[scrape] entry_id={raw.get('entry_id')} ver={raw.get('eft_version')}")

    entries = load_entries()
    existing_ids = {e.get("entry_id") for e in entries}
    is_new = raw.get("entry_id") not in existing_ids
    locked_now = scraper.is_locked(raw.get("raw_text", ""))

    # 같은 변경이 해시 ID/숫자 ID 로 이중 수집되는 것 방지(2차 dedupe)
    if is_new and not force:
        twin = find_twin(entries, raw)
        if twin is not None:
            new_id = str(raw.get("entry_id") or "")
            twin_id = str(twin.get("entry_id") or "")
            if new_id.isdigit() and twin_id.startswith("h"):
                # 숫자 ID 확보 — 기존 해시 항목을 재해석 없이 승격(영구 링크 확보)
                twin["entry_id"] = new_id
                twin["source_url"] = scraper.VIEW_URL.format(id=new_id)
                print(f"[dedupe] 해시 항목 {twin_id} → view id {new_id} 로 승격")
            else:
                print(f"[dedupe] 같은 변경(posted_at/버전 동일)이 이미 있음({twin_id}) — 건너뜀")
            is_new = False

    # 패치노트는 LLM 해석이 필요할 때만(잠금/재처리 포함) 1회 수집
    notes_cache: list[dict] | None = None

    def get_notes() -> list[dict]:
        nonlocal notes_cache
        if notes_cache is None:
            notes_cache = pn.get_patch_notes()
            print(f"[patchnotes] 후보 {len(notes_cache)}건")
        return notes_cache

    # 2) 최신 변경 처리
    if locked_now and is_new:
        # 접근 제한 상태 — LLM 호출 없이 '공개 대기'로 보류(쓸데없는 placeholder 해석 방지)
        entries.append(make_locked_entry(raw))
        print(f"[lock] 최신({raw.get('entry_id')})이 접근 제한 — 공개 대기로 보류")
    elif is_new or force:
        try:
            processed = interp.interpret(raw, get_notes())
        except Exception as ex:  # noqa: BLE001
            # LLM 쪽 장애로 그날 항목이 통째로 빠지지 않게 '해석 대기'로 보류.
            # locked=True 라 숫자 ID면 다음 실행의 reprocess_locked 가 자동 재시도한다.
            print(f"[interpret] 실패 — 해석 보류로 저장, 다음 실행 때 재시도: {ex}")
            held = make_locked_entry(raw)
            held["summary_ko"] = ("⏳ 해석 대기 중 — 자동 해석이 일시적으로 실패해 "
                                  "다음 갱신 때 다시 시도합니다.")
            held["tags"] = ["해석대기"]
            held["patch_note"]["reason_ko"] = "LLM 호출 실패로 아직 분석하지 않았습니다."
            entries = [e for e in entries if e.get("entry_id") != held.get("entry_id")]
            entries.append(held)
        else:
            flag = "잠수함패치" if processed.get("is_submarine") else "공지연결됨"
            print(f"[interpret] {flag} / {processed.get('summary_ko','')[:40]}")
            entries = [e for e in entries if e.get("entry_id") != processed.get("entry_id")]
            entries.append(processed)
    else:
        print("[skip] 이미 처리된 변경입니다. (신규 없음)")

    # 3) 이전에 보류된 '공개 대기' 항목 재처리(잠금 풀렸으면 실제 해석으로 교체)
    if any(e.get("locked") for e in entries):
        reprocess_locked(entries, get_notes())

    # 4) 안정성 자동 판정 → 저장 → 피드 재생성
    finalize(entries)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--from-file")
    args = ap.parse_args()
    return run(force=args.force, from_file=args.from_file)


if __name__ == "__main__":
    raise SystemExit(main())
