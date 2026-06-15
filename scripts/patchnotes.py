#!/usr/bin/env python3
"""
공식 패치노트 수집 모듈

LLM 매칭 단계에 넘길 '최근 공식 패치노트 후보 목록'을 만든다.
출처 두 가지를 합친다.

1) 수동 목록 파일  data/patchnotes_manual.json   (사용자가 직접 추가/보정)
   [{"title": "...", "url": "...", "date": "2026-06-12", "summary": "..."}]

2) 자동 수집  PATCHNOTES_URL 환경변수(기본: 공식 EFT 뉴스)에서 최근 글 제목/링크 파싱.
   사이트 구조가 바뀌거나 차단되면 조용히 건너뛰고 수동 목록만 사용한다.

자동 수집은 '깨져도 서비스가 멈추지 않도록' best-effort 로만 동작한다.
매칭 후보가 없으면 모든 변경이 기본적으로 '잠수함 패치'로 분류된다(설계 의도).
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

DEFAULT_PATCHNOTES_URL = os.environ.get(
    "PATCHNOTES_URL", "https://www.escapefromtarkov.com/news"
)
MANUAL_PATH = Path(__file__).resolve().parent.parent / "data" / "patchnotes_manual.json"
HEADERS = {"User-Agent": "TarkovKoreanChanges/1.0 (+github pages static site)"}


def _load_manual() -> list[dict]:
    if MANUAL_PATH.exists():
        try:
            return json.loads(MANUAL_PATH.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def _fetch_auto(url: str) -> list[dict]:
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
        return uniq[:20]
    except Exception as e:  # noqa: BLE001
        print(f"[patchnotes] 자동 수집 실패(무시): {e}")
        return []


def get_patch_notes(url: str | None = None) -> list[dict]:
    manual = _load_manual()
    auto = _fetch_auto(url or DEFAULT_PATCHNOTES_URL)
    # 수동 목록을 앞에 둬서 우선 노출(품질이 높음)
    return manual + auto


if __name__ == "__main__":
    import sys

    json.dump(get_patch_notes(), sys.stdout, ensure_ascii=False, indent=2)
    print(file=sys.stderr)
