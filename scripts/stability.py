#!/usr/bin/env python3
"""
안정성 판정 모듈

tarkov-changes diff(raw_text)를 (파일, 키경로, 이전값, 새값) 단위로 파싱하고,
전체 이력에서 '같은 키가 이후에 또 바뀌었는지 / 값이 되돌아갔는지(토글)'를 보고
각 변경 항목의 안정성을 자동 판정한다.

판정 결과(stability):
  - "stable"     : 이 변경 이후 동일 키가 다시 바뀐 적 없음 → 현재까지 유지
  - "superseded" : 이후 동일 키가 다시 바뀜(되돌이는 아님) → 값이 더 갱신됨
  - "recurring"  : 동일 키 값이 이전 상태로 되돌아오는 토글이 관찰됨 → 반복(이벤트성)
  - "unknown"    : diff 에서 키를 하나도 파싱하지 못함 → 판정 불가(배지·카운트 제외)

부가:
  - recurring 이면 recurring_event=True 로 표시할 수 있다(주말 부스트 등).
  - stability_detail_ko : 사람이 읽는 근거 한 줄.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Iterable

# 순수 경로 라인:  들여쓰기 + ['key']  (단, +/- 마커나 (Old)/(New) 가 없는 줄)
_PATH_RE = re.compile(r"^([ \t]*)\['(.*?)'\]\s*$")
# 파일 라인:  client/.../response.json   (들여쓰기 없음, 브래킷 없음)
_FILE_RE = re.compile(r"^([\w./-]+\.json)\s*$")
# 값 라인:  (앞에 +/- 가능) ... (Old)|(New) <값>
_VAL_RE = re.compile(r"^[+\-]?\s*\((Old|New)\)\s?(.*)$")
# 끝에 붙는 (+12.34%) / (-100.00%) 류 주석 제거용
_PCT_RE = re.compile(r"\s*\([+\-][\d.]+%\)\s*$")


def _norm(val: str) -> str:
    """값 비교용 정규화: 퍼센트 주석 제거 + 공백 축약."""
    val = _PCT_RE.sub("", val or "")
    return re.sub(r"\s+", " ", val).strip()


def parse_changes(raw_text: str) -> list[dict]:
    """raw diff → [{key, old, new, op}]  (key = '파일/경로/...')."""
    lines = (raw_text or "").splitlines()
    cur_file = None
    stack: list[tuple[int, str]] = []  # (indent, key)
    # path -> {old, new}  (같은 키에 Old/New 가 따로 등장하면 합친다)
    rec: dict[str, dict] = {}
    order: list[str] = []

    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue

        m = _FILE_RE.match(line.strip()) if "/" in line and "(" not in line else None
        if m and not line.startswith((" ", "\t", "+", "-")):
            cur_file = m.group(1)
            stack = []
            i += 1
            continue

        mp = _PATH_RE.match(line)
        if mp and "(Old)" not in line and "(New)" not in line:
            indent = len(mp.group(1))
            key = mp.group(2)
            while stack and stack[-1][0] >= indent:
                stack.pop()
            stack.append((indent, key))
            i += 1
            continue

        mv = _VAL_RE.match(line)
        if mv:
            kind = mv.group(1)
            inline = mv.group(2)
            # 멀티라인 JSON 값이면 다음 줄들을 모은다(경로/값/파일 라인 전까지)
            value_parts = [inline]
            j = i + 1
            if inline.strip().endswith("{") or inline.strip().endswith("["):
                while j < n:
                    nxt = lines[j]
                    if not nxt.strip():
                        j += 1
                        continue
                    if _VAL_RE.match(nxt) or _FILE_RE.match(nxt.strip()) or (
                        _PATH_RE.match(nxt) and "(Old)" not in nxt and "(New)" not in nxt
                    ):
                        break
                    value_parts.append(nxt)
                    j += 1
            path = cur_file + "/" + "/".join(k for _, k in stack) if cur_file else "/".join(k for _, k in stack)
            r = rec.get(path)
            if r is None:
                r = {"old": None, "new": None}
                rec[path] = r
                order.append(path)
            r["old" if kind == "Old" else "new"] = _norm(" ".join(value_parts))
            i = j
            continue

        i += 1

    out = []
    for path in order:
        r = rec[path]
        op = "modify"
        if r["old"] is None and r["new"] is not None:
            op = "add"
        elif r["new"] is None and r["old"] is not None:
            op = "remove"
        out.append({"key": path, "old": r["old"], "new": r["new"], "op": op})
    return out


_MONTHS = {m: i for i, m in enumerate(
    ("january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"), start=1)}
# 원본 게시 시각은 미 동부 시간(EDT/EST) 표기
_TZ_HOURS = {"EDT": -4, "EST": -5, "UTC": 0, "GMT": 0}
_POSTED_RE = re.compile(
    r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})(?:\s*-\s*(\d{1,2}):(\d{2})\s*([AP]M))?\s*([A-Z]{3,4})?", re.I
)


def entry_time(entry: dict) -> float:
    """정렬용 시각(UTC epoch 초). 원본 게시 시각(posted_at) 우선, 없으면 수집 시각(scraped_at), 둘 다 없으면 0.

    예: "Tuesday, 29 September 2026 - 11:41 AM EDT"
    """
    m = _POSTED_RE.search(entry.get("posted_at") or "")
    if m and m.group(2).lower() in _MONTHS:
        hour = int(m.group(4) or 12)
        ampm = (m.group(6) or "").upper()
        if ampm == "PM" and hour < 12:
            hour += 12
        elif ampm == "AM" and hour == 12:
            hour = 0
        try:
            local = datetime(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1)),
                             hour, int(m.group(5) or 0))
        except ValueError:
            local = None
        if local is not None:
            offset = _TZ_HOURS.get((m.group(7) or "").upper(), -5)
            return (local - timedelta(hours=offset)).replace(tzinfo=timezone.utc).timestamp()
    try:
        return datetime.strptime(entry.get("scraped_at") or "", "%Y-%m-%dT%H:%M:%SZ") \
            .replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return 0.0


def _order_key(entry: dict):
    """타임라인 순서: 원본 게시 시각 → 숫자 view id → 해시 id.

    예전에는 숫자 ID 를 전부 앞에, 해시 ID 를 전부 뒤에 둬 7월 해시 항목이 9월 숫자 항목보다
    '나중'으로 취급됐다(재변경·토글 판정 역전). ID 종류와 무관하게 시각으로 정렬한다.
    """
    eid = str(entry.get("entry_id", ""))
    return (entry_time(entry), int(eid) if eid.isdigit() else float("inf"), eid)


def build_index(entries: Iterable[dict]) -> dict[str, list[dict]]:
    """key -> 시간순 [{entry_id, old, new, op, order}] 타임라인."""
    idx: dict[str, list[dict]] = {}
    rows = sorted(entries, key=_order_key)
    for pos, e in enumerate(rows):
        for c in parse_changes(e.get("raw_text", "")):
            idx.setdefault(c["key"], []).append(
                {"entry_id": str(e.get("entry_id")), "old": c["old"], "new": c["new"], "op": c["op"], "pos": pos}
            )
    return idx


def toggling_keys(index: dict[str, list[dict]]) -> set[str]:
    """전체 타임라인에서 '새 값이 이전에 본 값으로 되돌아오는' 토글 키 집합."""
    out: set[str] = set()
    for key, tl in index.items():
        seen: set[str] = set()
        for ev in tl:  # 이미 시간순
            if ev["new"] is not None and ev["new"] in seen:
                out.add(key)
                break
            for v in (ev["old"], ev["new"]):
                if v:
                    seen.add(v)
    return out


# 변경 키 중 토글 키가 이 비율 이상이면 '반복(이벤트성)' 으로 본다.
RECURRING_RATIO = 0.5


def assess(entry: dict, index: dict[str, list[dict]], toggling: set[str] | None = None) -> dict:
    """이 entry 의 변경들을 index(전체 이력) 기준으로 판정.

    - recurring : 변경 키의 과반이 '전체 타임라인에서 토글'하는 키 → 반복/이벤트성
    - superseded: 이후 동일 키가 다시 바뀜(토글 과반은 아님)
    - stable    : 이 변경 이후 동일 키가 다시 바뀐 적 없음
    """
    if toggling is None:
        toggling = toggling_keys(index)

    my_changes = parse_changes(entry.get("raw_text", ""))
    eid = str(entry.get("entry_id"))
    my_pos = None
    for c in my_changes:
        for ev in index.get(c["key"], []):
            if ev["entry_id"] == eid:
                my_pos = ev["pos"]
                break
        if my_pos is not None:
            break

    total = len(my_changes)
    recurring_keys = [c["key"] for c in my_changes if c["key"] in toggling]
    later_changes = 0
    for c in my_changes:
        tl = index.get(c["key"], [])
        later_changes += sum(1 for ev in tl if my_pos is not None and ev["pos"] > my_pos)

    frac = (len(recurring_keys) / total) if total else 0.0

    if total == 0:
        # 키를 하나도 파싱하지 못했으면 '재변경 없음'이 아니라 판단 근거가 없는 것 — stable 로 두지 않는다
        stability = "unknown"
        detail = "변경 내용을 키 단위로 해석하지 못해 안정성을 판정하지 않았습니다."
    elif frac >= RECURRING_RATIO:
        stability = "recurring"
        detail = (
            f"변경 키 {total}개 중 {len(recurring_keys)}개가 값이 이전 상태로 되돌아오는 "
            f"토글 키({frac*100:.0f}%) — 반복/이벤트성으로 자동 판정."
        )
    elif later_changes > 0:
        # README 정의: superseded = 이후 같은 키가 또 바뀜. 토글 키가 일부(과반 미만) 섞였다는 것만으로는
        # 해당하지 않는다 — 예전엔 최신 항목도 '이후 0회 변경됨 — 값이 더 갱신됨'으로 모순 판정됐음
        stability = "superseded"
        note = f"토글 키 {len(recurring_keys)}개" if recurring_keys else "되돌이는 아님"
        detail = f"이후 동일 키가 다시 {later_changes}회 변경됨({note}) — 값이 더 갱신됨."
    else:
        stability = "stable"
        detail = "이 변경 이후 동일 키가 다시 바뀐 적 없음 — 현재까지 유지로 추정."

    return {
        "stability": stability,
        "stability_detail_ko": detail,
        "recurring_event": stability == "recurring",
        "stability_stats": {
            "changed_keys": total,
            "recurring_keys": len(recurring_keys),
            "later_changes": later_changes,
        },
    }


if __name__ == "__main__":
    import json
    import sys
    from pathlib import Path

    ROOT = Path(__file__).resolve().parent.parent
    src = json.loads((ROOT / "data" / "history_raw.json").read_text(encoding="utf-8"))
    index = build_index(src)
    targets = sys.argv[1:] or ["1094", "1068", "1065", "1051", "991", "1005", "1027", "1059", "1086"]
    by_id = {str(r["entry_id"]): r for r in src}
    for t in targets:
        e = by_id.get(t)
        if not e:
            print(f"{t}: (없음)")
            continue
        a = assess(e, index)
        print(f"id{t:>5} {a['stability']:<10} {a['stability_stats']} :: {a['stability_detail_ko']}")
