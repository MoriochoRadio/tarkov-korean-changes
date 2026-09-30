#!/usr/bin/env python3
"""
Tarkov Silent Changes 스크래퍼

1차 소스는 공개 목록 /list(최근 50건의 view id·버전·게시 시각)와 개별 페이지 /view/{id} 다.
/latest 는 최신 1건만 보여주고(하루 2건 이상이면 앞의 것을 놓침) 잠금 중엔 /list 로 리다이렉트돼
view id 없이 수집되는 문제가 있어, 목록 수집이 실패했을 때의 폴백으로만 쓴다.

출력 스키마(raw):
{
  "scraped_at": "2026-06-15T12:00:00Z",
  "source_url": "https://changes.tarkov-changes.com/latest",
  "eft_version": "1.0.5.0.45464",
  "posted_at": "Friday, 12 June 2026 - 12:06 PM EDT",
  "files_changed": [{"path": "client/globals/response.json", "count": 7}],
  "raw_text": "<페이지에서 추출한 diff 본문 전체>",
  "entry_id": "1084"        # /view/{id} 가 있으면 채움, 없으면 날짜 기반 해시
}
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

SOURCE_URL = "https://changes.tarkov-changes.com/latest"
LIST_URL = "https://changes.tarkov-changes.com/list"
VIEW_URL = "https://changes.tarkov-changes.com/view/{id}"
HEADERS = {
    "User-Agent": "TarkovKoreanChanges/1.0 (+github pages static site; respectful daily fetch)"
}

# 소스가 게시 후 12시간 동안 콘텐츠를 디스코드 로그인 사용자에게만 공개하는 경우
# 보이는 안내 문구. 이 문구가 있으면 실제 diff 를 받지 못한 '잠금' 상태다.
LOCK_MARKERS = ("Access Restricted", "not logged in", "Content available 12 hours")

# 일시적 서버 오류(게이트웨이/과부하)에 해당하는 상태 코드. 재시도 대상.
# 530: Cloudflare 가 원본 서버에 닿지 못할 때(오리진 다운) 돌려주는 코드 — 일시 장애로 취급
RETRY_STATUS = {429, 500, 502, 503, 504, 530}
# 2026-06-19: 총 21초 백오프로는 수 분짜리 502 장애를 못 넘겨 런이 실패했음.
# 하루 1회 실행이므로 몇 분 기다리는 비용은 무시 가능 — 총 ~2.5분까지 버틴다.
MAX_RETRIES = 5          # 최초 1회 + 재시도. 총 시도 횟수.
BACKOFF_BASE = 10.0      # 대기 시간(초): 10, 20, 40, 80 ... 지수 백오프.


def fetch_html(url: str = SOURCE_URL) -> str:
    """페이지 HTML을 가져온다.

    외부 소스가 502/503 같은 일시 장애나 네트워크 오류를 내는 경우가 있어,
    일시적 오류에 한해 지수 백오프로 재시도한다. 영구 오류(404 등)는 즉시 중단.
    """
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            if resp.status_code in RETRY_STATUS:
                resp.raise_for_status()  # 아래 except 에서 재시도 처리
            resp.raise_for_status()      # 그 외 오류 코드는 그대로 예외
            return resp.text
        except (requests.exceptions.HTTPError,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as exc:
            # HTTPError 중 재시도 대상이 아닌 코드는 즉시 중단
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if isinstance(exc, requests.exceptions.HTTPError) and status not in RETRY_STATUS:
                raise
            last_exc = exc
            if attempt < MAX_RETRIES:
                wait = BACKOFF_BASE * (2 ** (attempt - 1))
                print(
                    f"[scrape] 일시적 오류(시도 {attempt}/{MAX_RETRIES}, "
                    f"status={status}): {exc} -> {wait:.0f}초 후 재시도",
                    file=sys.stderr,
                )
                time.sleep(wait)
    # 모든 재시도 소진
    raise RuntimeError(
        f"{MAX_RETRIES}회 시도 후에도 소스를 가져오지 못했습니다: {url}"
    ) from last_exc


# 게시 시각 표기: "Tuesday, 29 September 2026 - 11:41 AM EDT"
_DATE_PAT = r"[A-Za-z]+,\s*\d{1,2}\s+[A-Za-z]+\s+\d{4}\s*-\s*[\d:]+\s*[AP]M\s*\w+"


def parse_list(html: str) -> list[dict]:
    """/list 페이지의 행들을 [{entry_id, eft_version, posted_at, change_count, source_url}] 로(최신순).

    잠금 중 /latest 가 리다이렉트되는 페이지도 같은 목록이라 그대로 파싱된다.
    """
    soup = BeautifulSoup(html, "html.parser")
    rows: list[dict] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=re.compile(r"/view/\d+")):
        vid = re.search(r"/view/(\d+)", a["href"]).group(1)
        text = " ".join(a.get_text(" ").split())  # "1.1.5.1.47510 - Tuesday, 29 September 2026 - 11:41 AM EDT"
        ver = _search(r"([0-9]+(?:\.[0-9]+){2,})", text)
        posted = _search(f"({_DATE_PAT})", text)
        if vid in seen or not (ver and posted):
            continue  # 목록 행이 아닌 링크(퍼머링크 등)
        seen.add(vid)
        li = a.find_parent("li")
        cnt = _search(r"(\d+)\s*change", " ".join(li.get_text(" ").split()) if li else "")
        rows.append({
            "entry_id": vid,
            "eft_version": ver,
            "posted_at": " ".join(posted.split()),
            "change_count": int(cnt) if cnt else None,
            "source_url": VIEW_URL.format(id=vid),
        })
    rows.sort(key=lambda r: int(r["entry_id"]), reverse=True)
    return rows


def parse(html: str, source_url: str = SOURCE_URL) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    # 화면에 보이는 텍스트만 추출(스크립트/스타일 제거)
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text("\n")
    # 빈 줄 정리
    lines = [ln.rstrip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln.strip() != ""]
    body = "\n".join(lines)

    eft_version = _search(r"EFT Version[:\s]*([0-9][0-9.]+)", body)
    posted_at = _search(rf"Dated[:\s]*({_DATE_PAT})", body)

    files_changed = []
    for m in re.finditer(r"([\w./-]+\.json)\s*\((\d+)\s*changes?\)", body):
        files_changed.append({"path": m.group(1), "count": int(m.group(2))})

    # diff 본문: "Files Changed" 이후 부분을 우선 사용, 없으면 전체 본문
    diff_text = body
    idx = body.find("Files Changed")
    if idx != -1:
        diff_text = body[idx:]

    # /view/{id} 링크가 있으면 entry_id 로 사용
    entry_id = None
    link = soup.find("a", href=re.compile(r"/view/(\d+)"))
    if link:
        entry_id = re.search(r"/view/(\d+)", link["href"]).group(1)

    # 잠금 중에는 /latest·/view 가 /list 로 리다이렉트돼 "EFT Version:"/"Dated:" 가 없다.
    # 버전·게시 시각을 비워 두면 잠금이 풀린 뒤 같은 변경을 알아보지 못해 중복 레코드가 생겼다
    # → 같은 페이지의 목록 행(대상 view id 의 행, /latest 면 맨 위 행)에서 채운다.
    if is_locked(body) and not (eft_version and posted_at):
        rows = parse_list(html)
        want = re.search(r"/view/(\d+)", source_url or "")
        if want:
            row = next((r for r in rows if r["entry_id"] == want.group(1)), None)
        else:
            row = rows[0] if rows else None
        if row:
            if not want:
                entry_id = row["entry_id"]
            eft_version = eft_version or row["eft_version"]
            posted_at = posted_at or row["posted_at"]

    if not entry_id:
        seed = f"{eft_version}|{posted_at}|{body[:200]}"
        entry_id = "h" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:10]

    return {
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_url": source_url,
        "eft_version": eft_version,
        "posted_at": posted_at,
        "files_changed": files_changed,
        "raw_text": diff_text.strip(),
        "entry_id": entry_id,
    }


def _search(pattern: str, text: str) -> str | None:
    m = re.search(pattern, text)
    return m.group(1).strip() if m else None


def is_locked(raw_text: str | None) -> bool:
    """소스가 접근 제한(로그인 게이트) 안내만 돌려준 상태인지 판정."""
    if not raw_text:
        return False
    return any(marker in raw_text for marker in LOCK_MARKERS)


def has_diff(raw_text: str | None) -> bool:
    """실제 변경 본문(Files Changed 섹션)이 들어있는지."""
    return bool(raw_text) and "Files Changed" in raw_text


def scrape(url: str = SOURCE_URL) -> dict:
    return parse(fetch_html(url), url)


def scrape_list(url: str = LIST_URL) -> list[dict]:
    """공개 목록(최근 50건)의 id·버전·게시 시각. 비어 있으면 사이트 구조가 바뀐 것."""
    return parse_list(fetch_html(url))


def scrape_view(entry_id: str | int) -> dict:
    """/view/{id} 에서 특정 변경을 직접 수집한다(잠금 해제 후 재처리용).

    /view 페이지에는 self-link 가 없어 parser 가 entry_id 를 해시로 채우므로
    실제 view id 로 덮어쓴다.
    """
    url = VIEW_URL.format(id=entry_id)
    raw = parse(fetch_html(url), url)
    raw["entry_id"] = str(entry_id)
    raw["source_url"] = url
    return raw


if __name__ == "__main__":
    data = scrape()
    json.dump(data, sys.stdout, ensure_ascii=False, indent=2)
    print()
