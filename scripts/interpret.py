#!/usr/bin/env python3
"""
LLM 해석 모듈

스크래핑된 raw 변경 + 최근 공식 패치노트 목록을 받아,
한글 번역/게임 의미 해석 + 패치노트 매칭(잠수함 패치 판별)을 수행한 뒤
구조화된 "processed entry" JSON 을 돌려준다.

제공자(provider):
  - github (기본)     : GitHub Models. 환경변수 GITHUB_TOKEN 사용(무료, 비용 0).
                        GitHub Actions 안에서는 자동 제공되는 토큰을 그대로 쓴다.
  - anthropic         : 환경변수 ANTHROPIC_API_KEY 필요(유료)
  - openai            : 환경변수 OPENAI_API_KEY 필요(유료)
환경변수 LLM_PROVIDER 로 선택. 모델은 LLM_MODEL 로 덮어쓸 수 있음.

키가 없거나 provider=stub 이면 결정론적 스텁 결과를 만들어 파이프라인이
끊기지 않게 한다(로컬 미리보기/CI 무키 테스트용).
"""
from __future__ import annotations

import json
import os
import re

# GitHub Models 엔드포인트(OpenAI 호환). 무료, 하루 1회 호출엔 한도 충분.
GITHUB_MODELS_BASE = "https://models.github.ai/inference"

DEFAULT_MODELS = {
    "github": "openai/gpt-4o",      # GitHub Models 는 'publisher/model' 형식
    "anthropic": "claude-opus-4-8",
    "openai": "gpt-4o",
}

SYSTEM_PROMPT = """당신은 '에스케이프 프롬 타르코프(EFT)' 게임의 데이터/밸런스 전문가이자 한국어 번역가입니다.
당신의 임무는 게임 클라이언트 설정 파일(JSON)에서 발견된 '사일런트(잠수함) 변경' diff 를
한국 일반 플레이어가 이해할 수 있도록 풀어 설명하는 것입니다.

원칙:
- 전문 용어는 한국 타르코프 커뮤니티에서 통용되는 표현을 우선 사용하되, 처음 나오면 괄호로 원어를 병기합니다.
- 추측이 필요한 부분은 단정하지 말고 "추정"이라고 명시합니다.
- 과장 없이 사실 위주로, 그러나 초보자도 '이게 게임에서 무슨 의미인지' 알 수 있게 씁니다.
- 반드시 유효한 JSON 하나만 출력합니다. 코드펜스나 설명 문장을 절대 덧붙이지 마세요."""

USER_TEMPLATE = """## 분석할 사일런트 변경 (raw diff)
EFT 버전: {eft_version}
게시 시각: {posted_at}
변경 파일: {files}

[RAW DIFF]
{raw_text}
[/RAW DIFF]

## 참고: 최근 공식 패치노트 (매칭 후보)
{patchnotes_block}

## 작업
위 diff 를 분석해 아래 JSON 스키마로만 답하세요.

{{
  "summary_ko": "전체 변경을 한 문장으로 요약",
  "tags": ["경험치/밸런스/아이템/맵/퀘스트/거래상/UI/기타 중 해당하는 것 1~3개"],
  "severity": "major 또는 minor 또는 trivial",
  "changes": [
    {{
      "key_path": "diff 의 키 경로",
      "before_ko": "이전 값/상태 설명",
      "after_ko": "변경된 값/상태 설명",
      "explanation_ko": "이 키/값이 게임에서 무엇을 의미하는지, 이번 변경이 무슨 뜻인지 2~4문장",
      "impact_ko": "플레이어 체감 영향 한 문장"
    }}
  ],
  "patch_note": {{
    "matched": true 또는 false,
    "title": "매칭된 패치노트 제목 또는 null",
    "url": "매칭된 패치노트 URL 또는 null",
    "reason_ko": "왜 연결했는지 또는 왜 잠수함 패치인지 근거 한 문장"
  }}
}}

patch_note.matched 가 false 이면 잠수함 패치입니다(코드에서 자동 처리).
"""


def _format_patchnotes(patch_notes: list[dict]) -> str:
    if not patch_notes:
        return "(수집된 공식 패치노트 없음 — 매칭 후보가 없으면 잠수함 패치로 판단)"
    out = []
    for p in patch_notes[:15]:
        title = p.get("title", "제목없음")
        date = p.get("date", "?")
        url = p.get("url", "")
        summary = p.get("summary", "")
        out.append(f"- [{date}] {title} — {url}\n  {summary}")
    return "\n".join(out)


def build_prompt(raw: dict, patch_notes: list[dict]) -> str:
    files = ", ".join(
        f"{f['path']} {f['count']}건" for f in raw.get("files_changed", [])
    ) or "미상"
    return USER_TEMPLATE.format(
        eft_version=raw.get("eft_version") or "미상",
        posted_at=raw.get("posted_at") or "미상",
        files=files,
        raw_text=(raw.get("raw_text") or "")[:12000],
        patchnotes_block=_format_patchnotes(patch_notes),
    )


# ---------- provider 호출 ----------
def _call_anthropic(system: str, user: str, model: str) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    msg = client.messages.create(
        model=model,
        max_tokens=4000,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")


def _call_openai(system: str, user: str, model: str) -> str:
    from openai import OpenAI

    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
    )
    return resp.choices[0].message.content


def _call_github(system: str, user: str, model: str) -> str:
    """GitHub Models (무료). OpenAI SDK 를 base_url 만 바꿔 재사용한다."""
    from openai import OpenAI

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    client = OpenAI(base_url=GITHUB_MODELS_BASE, api_key=token)
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return resp.choices[0].message.content


def _stub(raw: dict) -> dict:
    files = raw.get("files_changed") or [{"path": "unknown"}]
    return {
        "summary_ko": "(LLM 키 미설정) 자동 해석 대기 중 — 원문 diff 만 표시합니다.",
        "tags": ["기타"],
        "severity": "minor",
        "changes": [
            {
                "key_path": f.get("path", "unknown"),
                "before_ko": "-",
                "after_ko": "-",
                "explanation_ko": "API 키(또는 GitHub 토큰)가 설정되면 이 항목이 한글로 자동 해석됩니다.",
                "impact_ko": "-",
            }
            for f in files
        ],
        "patch_note": {
            "matched": False,
            "title": None,
            "url": None,
            "reason_ko": "키 미설정 상태이므로 매칭을 수행하지 않았습니다.",
        },
    }


def _extract_json(text: str) -> dict:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1:
        text = text[start:end + 1]
    return json.loads(text)


def interpret(raw: dict, patch_notes: list[dict] | None = None) -> dict:
    patch_notes = patch_notes or []
    provider = os.environ.get("LLM_PROVIDER", "github").lower()
    model = os.environ.get("LLM_MODEL") or DEFAULT_MODELS.get(provider, "")

    keys = {
        "github": os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"),
        "anthropic": os.environ.get("ANTHROPIC_API_KEY"),
        "openai": os.environ.get("OPENAI_API_KEY"),
    }
    has_key = bool(keys.get(provider))

    if provider == "stub" or not has_key:
        result = _stub(raw)
    else:
        prompt = build_prompt(raw, patch_notes)
        if provider == "github":
            text = _call_github(SYSTEM_PROMPT, prompt, model)
        elif provider == "openai":
            text = _call_openai(SYSTEM_PROMPT, prompt, model)
        else:
            text = _call_anthropic(SYSTEM_PROMPT, prompt, model)
        result = _extract_json(text)

    pn = result.get("patch_note") or {}
    result["is_submarine"] = not bool(pn.get("matched"))

    merged = {
        "entry_id": raw.get("entry_id"),
        "eft_version": raw.get("eft_version"),
        "posted_at": raw.get("posted_at"),
        "scraped_at": raw.get("scraped_at"),
        "source_url": raw.get("source_url"),
        "files_changed": raw.get("files_changed", []),
        "raw_text": raw.get("raw_text", ""),
        **result,
    }
    return merged


if __name__ == "__main__":
    import sys

    raw = json.load(sys.stdin)
    out = interpret(raw)
    json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
    print()
