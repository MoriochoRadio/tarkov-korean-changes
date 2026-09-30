#!/usr/bin/env python3
"""
공식 패치노트 수집 모듈

LLM 매칭 단계에 넘길 '공식 패치노트 후보 목록'을 만든다.
출처 두 가지를 합친다.

1) 수동 목록 파일  data/patchnotes_manual.json   (사용자가 직접 추가/보정)
   [{"title": "...", "url": "...", "date": "2026-06-12", "summary": "..."}]

2) 자동 수집  EFT 위키(fandom) 체인지로그 — MediaWiki API 로 공식 패치노트를 수록한 문서의
   위키텍스트를 받아 "==1.1.5.1.47510 (18 September 2026)==" 섹션별로 나눈다.
   공식 뉴스(escapefromtarkov.com/news)는 JS 렌더링 SPA 라 정적 HTML 에 글 링크가 없어
   (자매 레포 tarkov-companion 에서 2026-06 확인) 자동 수집이 한 번도 성공하지 못했다.
   PATCHNOTES_URL 을 지정하면 그 페이지의 글 링크도 추가로 긁는다(예전 방식, 정적 HTML 사이트용).

자동 수집은 '깨져도 서비스가 멈추지 않도록' best-effort 로만 동작한다.
매칭 후보가 없으면 모든 변경이 기본적으로 '잠수함 패치'로 분류된다(설계 의도).
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

WIKI_API = ("https://escapefromtarkov.fandom.com/api.php?action=parse&page=Changelog"
            "&prop=wikitext&format=json&formatversion=2")
WIKI_PAGE = "https://escapefromtarkov.fandom.com/wiki/Changelog"
WIKI_MAX_NOTES = 40        # 최신 섹션부터 이만큼(수개월치) — 과거 백필 항목의 매칭에도 쓰인다
SUMMARY_LIMIT = 600        # 공지 요약 길이 상한(프롬프트 토큰 절약)
MANUAL_PATH = Path(__file__).resolve().parent.parent / "data" / "patchnotes_manual.json"
HEADERS = {"User-Agent": "TarkovKoreanChanges/1.0 (+github pages static site)"}


def _load_manual() -> list[dict]:
    if MANUAL_PATH.exists():
        try:
            return json.loads(MANUAL_PATH.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def _clean_wikitext(s: str) -> str:
    """[[링크|표시명]] → 표시명, {{틀}}·강조 기호 제거."""
    s = re.sub(r"\[\[[^\]|]+\|([^\]]+)\]\]", r"\1", s)
    s = re.sub(r"\[\[([^\]]+)\]\]", r"\1", s)
    s = re.sub(r"\{\{[^}]*\}\}", "", s)
    s = re.sub(r"'{2,}", "", s)
    return " ".join(s.split())


def parse_changelog(wikitext: str, limit: int = WIKI_MAX_NOTES) -> list[dict]:
    """위키 체인지로그 위키텍스트 → [{title, url, date(YYYY-MM-DD), version, summary}] (최신순)."""
    notes: list[dict] = []
    for sec in re.split(r"^==(?!=)", wikitext or "", flags=re.M)[1:]:
        end = sec.find("==")
        if end < 0:
            continue
        heading = sec[:end].strip()                      # "1.1.5.1.47510 (18 September 2026)"
        m = re.search(r"\((\d{1,2} [A-Za-z]+ \d{4})\)", heading)
        if not m:
            continue
        try:
            date = datetime.strptime(m.group(1), "%d %B %Y").date().isoformat()
        except ValueError:
            continue
        parts: list[str] = []
        for line in sec[end + 2:].splitlines():
            line = line.strip()
            sub = re.fullmatch(r"=+\s*(.*?)\s*=+", line)
            if sub:                                      # ===League System=== → [League System]
                parts.append(f"[{_clean_wikitext(sub.group(1))}]")
            elif line.startswith("*"):
                text = _clean_wikitext(line.lstrip("*"))
                if text:
                    parts.append(text)
        if not any(not p.startswith("[") for p in parts):  # 불릿 없는 서술형 섹션 — 첫 문단을 요약으로
            para = next((ln.strip() for ln in sec[end + 2:].splitlines()
                         if ln.strip() and not ln.strip().startswith(("=", "{", "|", "["))), "")
            if para:
                parts.append(_clean_wikitext(para))
        summary = "; ".join(parts)
        if len(summary) > SUMMARY_LIMIT:
            summary = summary[:SUMMARY_LIMIT].rstrip() + "…"
        ver = re.match(r"([0-9]+(?:\.[0-9]+)+)", heading)
        notes.append({
            "title": f"공식 패치노트 {heading}",
            "url": f"{WIKI_PAGE}#{heading.replace(' ', '_')}",
            "date": date,
            "version": ver.group(1) if ver else "",
            "summary": summary,
        })
        if len(notes) >= limit:
            break
    return notes


def _fetch_wiki_changelog() -> list[dict]:
    try:
        import requests

        resp = requests.get(WIKI_API, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        notes = parse_changelog(resp.json()["parse"]["wikitext"])
        print(f"[patchnotes] 위키 체인지로그 {len(notes)}건 (최신 {notes[0]['title'] if notes else '-'})")
        return notes
    except Exception as e:  # noqa: BLE001
        print(f"[patchnotes] 위키 체인지로그 수집 실패(무시): {e}")
        return []


def _fetch_auto(url: str) -> list[dict]:
    """PATCHNOTES_URL 로 지정한 페이지의 글 링크를 긁는다(정적 HTML 에 링크가 있는 사이트용)."""
    try:
        import requests
        from bs4 import BeautifulSoup

        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        notes = []
        for a in soup.find_all("a", href=True):
            title = a.get_text(strip=True)
            href = a["href"]
            if not title or len(title) < 8:
                continue
            if re.search(r"(patch|notes|update|version|news|\d\.\d)", (title + href), re.I):
                if href.startswith("/"):
                    base = re.match(r"(https?://[^/]+)", url)
                    href = (base.group(1) if base else "") + href
                notes.append({"title": title, "url": href, "date": "", "summary": ""})
        # 중복 제거
        seen, uniq = set(), []
        for n in notes:
            if n["url"] in seen:
                continue
            seen.add(n["url"])
            uniq.append(n)
        print(f"[patchnotes] {url} 에서 링크 {len(uniq[:20])}건")
        return uniq[:20]
    except Exception as e:  # noqa: BLE001
        print(f"[patchnotes] 자동 수집 실패(무시): {e}")
        return []


def get_patch_notes(url: str | None = None) -> list[dict]:
    manual = _load_manual()
    # 워크플로는 미설정 Variable 을 빈 문자열로 넘기므로 `or` 로 판단
    custom = url or os.environ.get("PATCHNOTES_URL") or ""
    auto = _fetch_wiki_changelog() + (_fetch_auto(custom) if custom else [])
    # 수동 목록을 앞에 둬서 우선 노출(품질이 높음)
    return manual + auto


if __name__ == "__main__":
    import sys

    json.dump(get_patch_notes(), sys.stdout, ensure_ascii=False, indent=2)
    print(file=sys.stderr)
