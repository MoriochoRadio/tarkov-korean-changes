#!/usr/bin/env python3
"""
일일 파이프라인 오케스트레이터

순서:
  1) scrape   : changes.tarkov-changes.com/list(최근 50건의 view id·버전·게시 시각)로
                지난 실행 이후 새로 올라온 변경을 찾아 /view/{id} 로 수집
                (목록 수집이 안 되면 /latest 최신 1건으로 폴백)
  2) patchnotes: 공식 패치노트 후보 목록 확보(EFT 위키 체인지로그 + 수동 목록)
  3) interpret : LLM 으로 한글 해석 + 패치노트 매칭(잠수함 패치 판별)
  4) reprocess : 보류(공개 대기/해석 대기) 항목 재처리
  5) backfill  : 목록에는 있는데 아직 없는 과거 변경을 실행마다 BACKFILL_PER_RUN 건씩 자동 해석
  6) store     : data/entries.json 저장(같은 변경의 해시 ID/숫자 ID 이중 레코드는 숫자 ID 로 병합)
  7) build     : docs/data.json (사이트가 읽는 피드) 재생성

GitHub Actions 가 매일 이 스크립트를 실행하고, 변경분을 커밋한다.

옵션:
  --force         : 이미 저장된 entry_id 여도 다시 해석해 갱신(목록의 최신 1건)
  --from-file F   : 라이브 스크래핑 대신 raw JSON 파일에서 입력(테스트/시드용)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
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

HELD_TAG = "해석대기"  # LLM 실패로 보류된 항목 표식 — 원문은 이미 있으므로 재수집 없이 재해석
RAW_KEYS = ("entry_id", "eft_version", "posted_at", "scraped_at", "source_url",
            "files_changed", "raw_text")
STABILITY_FIELDS = ("stability", "stability_detail_ko", "recurring_event", "stability_stats")
# 보류(공개 대기/해석 대기) 항목이 이 기간 넘게 안 풀리면 재시도로 해결되지 않는 문제로 보고 알린다
STALE_HELD_DAYS = 3
# 목록(/list, 최근 50건)에는 있는데 저장되지 않은 과거 변경을 실행마다 이 건수까지 자동 해석한다.
# Groq 무료 한도(gpt-oss-120b 30 RPM·1,000 RPD·8,000 TPM)를 넉넉히 지키면서 며칠에 걸쳐 채워진다.
BACKFILL_PER_RUN = 2
# 한 실행에서 처리할 '새로 올라온' 변경 상한 — 장기 중단 뒤 LLM 호출 폭주 방지(나머지는 백필로 이월)
FRESH_MAX = 5
# 같은 실행에서 LLM 을 연달아 부를 때의 최소 간격(초). 큰 diff 한 건이 분당 토큰 한도(8,000 TPM)의
# 대부분을 쓰므로 1분 창을 비워 준다(429 → 하위 모델 강등·보류 방지).
LLM_GAP_SEC = 60
# 사람 조치가 필요한 상황(수집 장기 실패 등). interpret.NEEDS_HUMAN 과 합쳐 런 끝에 보고하고,
# ALERT_FILE 이 지정되면(워크플로) 거기에 기록 → 커밋·배포 뒤 마지막 단계가 런을 실패시킨다.
ALERTS: list[str] = []


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
        # 아직 분석 전이라 잠수함 여부를 모른다(True 로 두면 잠수함 카운트에 섞였음)
        "is_submarine": None,
    }


_LLM_LAST: dict = {"t": None}  # 이번 실행의 마지막 LLM 호출 시각(monotonic)


def interpret_paced(raw: dict, notes: list[dict]) -> dict:
    """LLM 해석. 같은 실행에서 연달아 부르면 LLM_GAP_SEC 간격을 둔다(Groq 분당 토큰 한도)."""
    last = _LLM_LAST["t"]
    if (last is not None and not interp.gave_up()
            and (os.environ.get("LLM_PROVIDER") or "").lower() != "stub"):
        wait = LLM_GAP_SEC - (time.monotonic() - last)
        if wait > 0:
            print(f"[pace] 분당 토큰 한도 보호 — {wait:.0f}초 후 다음 해석")
            time.sleep(wait)
    try:
        return interp.interpret(raw, notes)
    finally:
        _LLM_LAST["t"] = time.monotonic()


def reprocess_locked(entries: list[dict], notes: list[dict], skip: set[str] | None = None) -> bool:
    """이전에 보류된 항목을 다시 처리한다.

    - '공개 대기'(원본 잠금): /view/{id} 로 재수집해 잠금이 풀렸으면 LLM 해석으로 교체.
    - '해석 대기'(LLM 실패): 저장해 둔 원문으로 바로 재해석(재수집 불필요 — 해시 ID 도 복구).
    LLM 이 또 실패하면 보류를 유지하고 다음 실행에서 다시 시도한다(런은 계속 진행).
    정렬이 흔들리지 않도록 원래 scraped_at 은 보존한다.
    skip: 이번 실행에서 막 보류로 저장한 id(방금 잠김을 확인했으니 다시 요청하지 않는다).
    반환값: 하나라도 갱신했으면 True.
    """
    changed = False
    for i, e in enumerate(entries):
        if not e.get("locked"):
            continue
        eid = e.get("entry_id")
        if skip and str(eid) in skip:
            continue
        stored = e.get("raw_text") or ""
        if (HELD_TAG in (e.get("tags") or []) and scraper.has_diff(stored)
                and not scraper.is_locked(stored)):
            raw = {k: e.get(k) for k in RAW_KEYS}
        else:
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
        try:
            processed = interpret_paced(raw, notes)
        except Exception as ex:  # noqa: BLE001
            print(f"[reprocess] {eid}: 해석 실패(보류 유지, 다음 실행 때 재시도) — {ex}")
            continue
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
        if e.get("locked"):
            # 보류(공개/해석 대기) 항목은 diff 가 없거나 아직 해석 전 — 판정하지 않는다(배지·카운트 제외)
            for k in STABILITY_FIELDS:
                e.pop(k, None)
            continue
        e.update(stab.assess(e, index, toggling))
    return entries


def finalize(entries: list[dict]) -> None:
    """안정성 주석 → entries.json 저장 → docs/data.json 재생성."""
    annotate_stability(entries)
    save_entries(entries)
    build_feed(entries)


def build_feed(entries: list[dict]) -> None:
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    # 최신순 정렬 — 원본 게시 시각 기준(자동 백필 항목은 수집 시각이 게시보다 한참 늦다)
    ordered = sorted(entries, key=stab.entry_time, reverse=True)[:FEED_LIMIT]
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


def _when(e: dict) -> tuple[str, str] | None:
    """같은 변경의 식별 키: (게시 시각 — 공백 정규화, 게임 버전). 하나라도 없으면 None."""
    posted = " ".join((e.get("posted_at") or "").split())
    ver = (e.get("eft_version") or "").strip()
    return (posted, ver) if posted and ver else None


def find_twin(entries: list[dict], raw: dict) -> dict | None:
    """같은 변경이 해시 ID와 숫자 ID 로 두 번 수집되는 것을 posted_at+eft_version 으로 탐지."""
    key = _when(raw)
    if key is None:
        return None
    for e in entries:
        if _when(e) == key and e.get("entry_id") != raw.get("entry_id"):
            return e
    return None


def _take_interpretation(target: dict, src: dict) -> None:
    """보류 중인 target(숫자 ID)에 같은 변경의 해석 완료본 src 를 옮긴다 — 재해석 불필요."""
    keep = {k: target.get(k) for k in ("entry_id", "source_url", "scraped_at")}
    target.clear()
    target.update(src)
    target.update({k: v for k, v in keep.items() if v})
    target.pop("locked", None)


def merge_twins(entries: list[dict]) -> list[dict]:
    """같은 변경이 해시 ID(예전 /latest 수집)와 숫자 ID 로 이중 저장된 레코드를 하나로 합친다(멱등).

    첫날 원본이 잠겨 숫자 ID 의 '공개 대기'가 버전·게시 시각 없이 저장되고, 다음 날 /latest 에서
    view 링크 없이 해시 ID 로 또 해석되던 버그의 흔적이다. 숫자 ID(영구 링크) 레코드를 남기되,
    숫자 쪽이 아직 보류(locked)이고 해시 쪽이 해석을 갖고 있으면 그 해석을 숫자 쪽으로 옮긴다.
    """
    numeric: dict = {}
    for e in entries:
        key = _when(e)
        if key and str(e.get("entry_id") or "").isdigit():
            numeric.setdefault(key, e)
    out = []
    for e in entries:
        eid = str(e.get("entry_id") or "")
        twin = None if eid.isdigit() else numeric.get(_when(e))
        if twin is None:
            out.append(e)
            continue
        if twin.get("locked") and not e.get("locked"):
            _take_interpretation(twin, e)
            print(f"[dedupe] {eid} 의 해석을 보류 중인 {twin['entry_id']} 로 옮기고 중복 제거")
        else:
            print(f"[dedupe] {eid} 는 {twin['entry_id']} 와 같은 변경 — 중복 레코드 제거")
    return out


def reconcile_with_list(entries: list[dict], listing: list[dict]) -> list[dict]:
    """목록(/list)의 view id·버전·게시 시각으로 기존 레코드를 보정한다.

    - 버전·게시 시각 없이 저장된 보류 항목(예전 잠금 placeholder)에 목록 값을 채운다
    - 해시 ID 레코드가 목록의 (게시 시각, 버전)과 일치하면 숫자 ID 로 승격(영구 링크 + 백필 중복 방지)
    - 그 결과 드러난 해시/숫자 이중 레코드는 merge_twins 로 합친다
    """
    by_id = {r["entry_id"]: r for r in listing}
    by_when = {_when(r): r for r in listing if _when(r)}
    have = {str(e.get("entry_id")) for e in entries}
    for e in entries:
        eid = str(e.get("entry_id") or "")
        if eid.isdigit():
            r = by_id.get(eid)
            for k in ("eft_version", "posted_at"):
                if r and not e.get(k):
                    e[k] = r[k]
            continue
        r = by_when.get(_when(e))
        if r and r["entry_id"] not in have:
            print(f"[dedupe] 해시 항목 {eid} → view id {r['entry_id']} 로 승격")
            e["entry_id"] = r["entry_id"]
            e["source_url"] = r["source_url"]
            have.add(r["entry_id"])
    return merge_twins(entries)


def _held_entry(raw: dict) -> dict:
    """LLM 실패로 '해석 대기' 보류 — locked=True 라 다음 실행의 reprocess_locked 가 저장된 원문으로 재시도."""
    held = make_locked_entry(raw)
    held["summary_ko"] = ("⏳ 해석 대기 중 — 자동 해석이 일시적으로 실패해 "
                          "다음 갱신 때 다시 시도합니다.")
    held["tags"] = [HELD_TAG]
    held["patch_note"]["reason_ko"] = "LLM 호출 실패로 아직 분석하지 않았습니다."
    return held


def store_new(entries: list[dict], raw: dict, get_notes) -> list[dict]:
    """새 변경 1건: 잠겨 있으면 '공개 대기', LLM 실패면 '해석 대기'로 보류, 아니면 해석해 저장."""
    eid = raw.get("entry_id")
    if scraper.is_locked(raw.get("raw_text", "")):
        if any(e.get("entry_id") == eid for e in entries):
            print(f"[lock] {eid}: 접근 제한 — 기존 레코드 유지")
            return entries
        # 접근 제한 상태 — LLM 호출 없이 '공개 대기'로 보류(쓸데없는 placeholder 해석 방지)
        entries.append(make_locked_entry(raw))
        print(f"[lock] {eid}({raw.get('posted_at')}) 접근 제한 — 공개 대기로 보류")
        return entries
    try:
        processed = interpret_paced(raw, get_notes())
    except Exception as ex:  # noqa: BLE001
        # LLM 쪽 장애로 그날 항목이 통째로 빠지지 않게 '해석 대기'로 보류.
        print(f"[interpret] {eid}: 실패 — 해석 보류로 저장, 다음 실행 때 재시도: {ex}")
        processed = _held_entry(raw)
    else:
        flag = "잠수함패치" if processed.get("is_submarine") else "공지연결됨"
        print(f"[interpret] {eid}: {flag} / {processed.get('summary_ko', '')[:40]}")
    entries = [e for e in entries if e.get("entry_id") != eid]
    entries.append(processed)
    return entries


def fetch_row(row: dict) -> dict:
    """목록 행의 /view/{id} 수집. 페이지에 버전·게시 시각이 없으면(잠금 등) 목록 값으로 채운다."""
    raw = scraper.scrape_view(row["entry_id"])
    for k in ("eft_version", "posted_at"):
        if not raw.get(k):
            raw[k] = row.get(k)
    return raw


def process_listing(entries: list[dict], listing: list[dict], force: bool,
                    get_notes) -> tuple[list[dict], set[str]]:
    """지난 실행 이후 목록에 새로 올라온 변경(저장된 최대 view id 보다 큰 것)을 모두 처리한다.

    /latest 는 최신 1건만 보여줘 하루 2건 이상 올라오면 앞의 것을 잃었다 — 목록 기준으로 전부 받는다.
    반환: (entries, 이번 실행에서 보류로 저장한 id — 같은 실행의 재처리에서 제외)
    """
    have = {str(e.get("entry_id")) for e in entries}
    known = [int(x) for x in have if x.isdigit()]
    newest = max(known) if known else 0
    fresh = [r for r in listing if r["entry_id"] not in have and int(r["entry_id"]) > newest]
    if len(fresh) > FRESH_MAX:
        print(f"[list] 새 변경 {len(fresh)}건 — 최신 {FRESH_MAX}건만 지금 처리(나머지는 백필로 이월)")
        fresh = fresh[:FRESH_MAX]
    if force and listing and listing[0] not in fresh:
        fresh.insert(0, listing[0])
    if not fresh:
        print("[skip] 목록에 새로 올라온 변경 없음")
    held: set[str] = set()
    for r in reversed(fresh):  # 오래된 것부터 게시 순서대로
        eid = r["entry_id"]
        try:
            raw = fetch_row(r)
        except Exception as ex:  # noqa: BLE001
            print(f"[scrape] {eid}: /view 수집 실패 — 다음 실행에서 다시 시도: {ex}")
            continue
        print(f"[scrape] entry_id={eid} ver={raw.get('eft_version')} @ {raw.get('posted_at')}")
        entries = store_new(entries, raw, get_notes)
        if any(e.get("entry_id") == eid and e.get("locked") for e in entries):
            held.add(eid)
    return entries, held


def backfill(entries: list[dict], listing: list[dict], get_notes,
             limit: int = BACKFILL_PER_RUN) -> list[dict]:
    """목록에는 있는데 저장되지 않은 과거 변경을 최신 것부터 실행마다 최대 limit 건 해석해 채운다.

    예전 /latest 방식에서 놓친 변경이 사람 손 없이 며칠에 걸쳐 채워진다. 해석까지 성공한 것만
    저장하므로(보류 레코드를 만들지 않음) 수집·해석에 실패한 건 다음 실행에서 자동으로 다시 시도된다.
    """
    have = {str(e.get("entry_id")) for e in entries}
    todo = [r for r in listing if r["entry_id"] not in have]
    if not todo or limit <= 0:
        return entries
    print(f"[backfill] 목록 기준 누락 {len(todo)}건 — 이번 실행에서 최대 {limit}건 해석")
    done = tried = 0
    for r in todo:
        if done >= limit or tried >= limit + 2:
            break
        if interp.gave_up():
            print(f"[backfill] 이번 실행은 LLM 해석 중단 상태 — 남은 백필은 다음 실행에서: {interp.gave_up()}")
            break
        tried += 1
        eid = r["entry_id"]
        try:
            raw = fetch_row(r)
        except Exception as ex:  # noqa: BLE001
            print(f"[backfill] {eid}: 수집 실패 — 다음 실행에서 다시 시도: {ex}")
            break
        rt = raw.get("raw_text", "")
        if scraper.is_locked(rt) or not scraper.has_diff(rt):
            print(f"[backfill] {eid}: 잠김/변경 본문 없음 — 건너뜀")
            continue
        try:
            processed = interpret_paced(raw, get_notes())
        except Exception as ex:  # noqa: BLE001
            print(f"[backfill] {eid}: 해석 실패 — 다음 실행에서 다시 시도: {ex}")
            continue
        processed["backfilled"] = True
        entries.append(processed)
        done += 1
        print(f"[backfill] {eid}({r['posted_at']}) 채움 — {processed.get('summary_ko', '')[:36]}")
    return entries


def process_latest(entries: list[dict], raw: dict, force: bool, get_notes) -> list[dict]:
    """/latest(목록 수집 실패 시 폴백) 또는 --from-file 로 받은 변경 1건 처리."""
    is_new = raw.get("entry_id") not in {e.get("entry_id") for e in entries}
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
    if is_new or force:
        return store_new(entries, raw, get_notes)
    print("[skip] 이미 처리된 변경입니다. (신규 없음)")
    return entries


def run(force: bool = False, from_file: str | None = None) -> int:
    entries = load_entries()
    # 예전 버그로 이미 쌓인 해시/숫자 이중 레코드 정리(멱등 — 없으면 아무 일도 안 함)
    entries = merge_twins(entries)

    # 패치노트는 LLM 해석이 필요할 때만(잠금/재처리 포함) 1회 수집
    notes_cache: list[dict] | None = None

    def get_notes() -> list[dict]:
        nonlocal notes_cache
        if notes_cache is None:
            notes_cache = pn.get_patch_notes()
            print(f"[patchnotes] 후보 {len(notes_cache)}건")
        return notes_cache

    # 1) 수집 — 목록(/list) 우선, 안 되면 /latest 1건으로 폴백
    listing: list[dict] = []
    held: set[str] = set()
    if from_file:
        raw = json.loads(Path(from_file).read_text(encoding="utf-8"))
        print(f"[scrape] 파일에서 입력: {from_file}")
        entries = process_latest(entries, raw, force, get_notes)
    else:
        try:
            listing = scraper.scrape_list()
            if not listing:
                print("[list] 목록에서 변경 행을 찾지 못함(사이트 구조 변경 의심) — /latest 로 폴백")
        except Exception as ex:  # noqa: BLE001
            print(f"[list] 목록 수집 실패 — /latest 로 폴백: {ex}")
        if listing:
            _scrape_failures(reset=True)
            top = listing[0]
            print(f"[list] 목록 {len(listing)}건 (최신 {top['entry_id']} @ {top['posted_at']})")
            entries = reconcile_with_list(entries, listing)
            entries, held = process_listing(entries, listing, force, get_notes)
        else:
            try:
                raw = scraper.scrape()
            except Exception as ex:  # noqa: BLE001
                # 업스트림 장애(수 분 이상 지속되는 502 등) — 기존 데이터로 피드는 재생성하고
                # 1회성이면 조용히 넘어간다. 목록이 최근 50건을 보여줘 하루 이틀 장애로는 유실되지
                # 않지만, 이틀 연속이면 차단·구조 변경일 수 있어 '사람 조치 필요'로 보고한다
                # (커밋은 그대로 진행되고, 워크플로 마지막 단계가 런을 실패시킨다).
                fails = _scrape_failures()
                print(f"[scrape] 실패({fails}일 연속): {ex}")
                if fails >= 2:
                    ALERTS.append(f"원본 수집 {fails}일 연속 실패(/list·/latest 모두) — "
                                  f"사이트 장애·차단·구조 변경 확인 필요: {ex}")
            else:
                _scrape_failures(reset=True)
                print(f"[scrape] (폴백 /latest) entry_id={raw.get('entry_id')} ver={raw.get('eft_version')}")
                entries = process_latest(entries, raw, force, get_notes)
                if scraper.is_locked(raw.get("raw_text", "")):
                    held.add(str(raw.get("entry_id")))

    # 2) 이전에 보류된 '공개/해석 대기' 항목 재처리(풀렸으면 실제 해석으로 교체).
    #    여기서 무엇이 실패해도 수집분 저장·커밋·배포는 계속되어야 한다.
    if any(e.get("locked") and str(e.get("entry_id")) not in held for e in entries):
        try:
            reprocess_locked(entries, get_notes(), skip=held)
        except Exception as ex:  # noqa: BLE001
            print(f"[reprocess] 보류 항목 재처리 실패(무시): {ex}")

    # 3) 과거 누락분 자동 백필(실행마다 소량)
    if listing:
        try:
            entries = backfill(entries, listing, get_notes)
        except Exception as ex:  # noqa: BLE001
            print(f"[backfill] 실패(무시): {ex}")

    # 4) 안정성 자동 판정 → 저장 → 피드 재생성
    finalize(entries)
    return 0


def stale_held(entries: list[dict], days: int = STALE_HELD_DAYS) -> list[str]:
    """보류 상태가 days 일 넘게 풀리지 않은 항목 — 재시도로 해결되지 않는 문제의 신호."""
    now = datetime.now(timezone.utc)
    out = []
    for e in entries:
        if not e.get("locked"):
            continue
        try:
            t = datetime.strptime(e.get("scraped_at") or "", "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue
        age = (now - t.replace(tzinfo=timezone.utc)).days
        if age >= days:
            out.append(f"{e.get('entry_id')}({'/'.join(e.get('tags') or [])} {age}일째)")
    return out


def report_alerts(entries: list[dict]) -> None:
    """사람 조치가 필요한 상황만 모아 출력하고, ALERT_FILE 이 있으면 기록한다.

    일시 오류는 여기 오지 않는다(재시도·보류로 흡수). 신규 변경이 없는 날도 정상.
    """
    stale = stale_held(entries)
    if stale:
        ALERTS.append(f"보류 항목이 {STALE_HELD_DAYS}일 넘게 풀리지 않음: {', '.join(stale)}")
    msgs = [m.replace("\n", " ") for m in dict.fromkeys(ALERTS + interp.NEEDS_HUMAN)]
    for m in msgs:
        print(f"[alert] 사람 조치 필요: {m}")
    path = os.environ.get("ALERT_FILE")
    if path and msgs:
        Path(path).write_text("\n".join(msgs) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--from-file")
    args = ap.parse_args()
    rc = run(force=args.force, from_file=args.from_file)
    report_alerts(load_entries())
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
