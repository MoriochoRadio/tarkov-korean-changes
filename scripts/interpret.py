#!/usr/bin/env python3
"""
LLM 해석 모듈

스크래핑된 raw 변경 + 최근 공식 패치노트 목록을 받아,
한글 번역/게임 의미 해석 + 패치노트 매칭(잠수함 패치 판별)을 수행한 뒤
구조화된 "processed entry" JSON 을 돌려준다.

제공자(provider):
  - groq (기본)       : Groq 무료 티어. 환경변수 GROQ_API_KEY 사용.
                        모델은 실행 때마다 활성 모델 목록(GET /models)을 조회해 선호 순서대로
                        자동 선택한다(GROQ_MODEL 이 있으면 최우선). 무료 모델은 수개월 단위로
                        폐지되므로(2026 여름: GitHub Models, llama-3.3-70b) 폐지·미존재 오류가
                        나면 코드 수정 없이 다음 후보로 넘어가고, 성공한 모델을 런 동안 고정한다.
  - anthropic         : 환경변수 ANTHROPIC_API_KEY 필요(유료)
  - openai            : 환경변수 OPENAI_API_KEY 필요(유료)
환경변수 LLM_PROVIDER 로 선택. 유료 제공자 모델은 LLM_MODEL 로 덮어쓸 수 있음.

provider=stub 이면 결정론적 스텁 결과를 만든다(로컬 미리보기/CI 무키 테스트용).
키 누락·인증 실패(401/403)·쓸 수 있는 모델 없음처럼 재시도로 풀리지 않는 상황은
NEEDS_HUMAN 에 모아 두고 LLMError 를 던진다 — 파이프라인은 항목을 '해석 대기'로
보류하고, 워크플로는 커밋·배포를 마친 뒤 마지막 단계에서 실패해 소유자에게 메일이 간다.
일시 오류(429/5xx/네트워크)는 재시도로 흡수하며 알림 대상이 아니다.
"""
from __future__ import annotations

import json
import os
import re
import time

# Groq 엔드포인트(OpenAI 호환). 무료 티어, 하루 1회 호출엔 한도 충분.
GROQ_BASE = "https://api.groq.com/openai/v1"
# urllib 기본 UA(Python-urllib/x.y)는 Groq 앞단 Cloudflare 가 403 으로 차단 — 식별 가능한 UA 명시
USER_AGENT = "TarkovKoreanChanges/1.0 (+https://github.com/MoriochoRadio/tarkov-korean-changes)"

# 선호 순서. 활성 목록에 있는 것만 이 순서로 쓰고, 목록 조회 자체가 실패하면 그대로 시도한다.
GROQ_PREFERRED = ["openai/gpt-oss-120b", "qwen/qwen3.8-27b", "openai/gpt-oss-20b"]
# 선호 목록 밖이라도 활성 목록에 있으면 뒤에 붙여 시도할 텍스트 모델 계열(앞쪽 우선, 같은 계열은 큰 모델 우선)
GROQ_FAMILIES = ("gpt-oss", "qwen", "kimi", "llama")
# 해석에 쓸 수 없는 모델(음성·TTS·안전 분류기·에이전트 시스템) — 이름에 포함되면 제외
GROQ_EXCLUDE = ("whisper", "tts", "playai", "orpheus", "audio", "guard", "compound", "embed")
TEMPERATURE = 0.2          # 번역·해석은 창의성보다 일관성 — 0~0.2 고정
RETRY_WAITS = (15, 30)     # 429/5xx/네트워크 오류 시 같은 모델 재시도 전 대기(초)
MAX_CANDIDATES = 6         # 한 런에서 시도할 최대 모델 수
LLM_BUDGET_SEC = 8 * 60    # 런 전체 LLM 시간 예산 — 잡 타임아웃(15분) 안에서 커밋할 여유 확보

DEFAULT_MODELS = {
    "anthropic": "claude-opus-4-8",
    "openai": "gpt-4o",
}

# 사람이 조치해야 하는 상황 메시지(중복 제거). pipeline 이 런 끝에 모아 워크플로에 넘긴다.
NEEDS_HUMAN: list[str] = []
# 런 동안 유지되는 Groq 선택 상태: 고정된 모델, 후보 목록, 폐지 판정 모델, 모델별 파라미터,
# 이번 런에서 포기했는지(이후 호출은 즉시 실패 → 보류), 시간 예산 기준점
_GROQ: dict = {"model": None, "candidates": None, "dead": set(), "params": {},
               "gave_up": None, "started": None}


class LLMError(RuntimeError):
    """LLM 해석 실패. needs_human=True 면 재시도로는 풀리지 않는 상황(키·모델 문제)."""

    def __init__(self, msg: str, needs_human: bool = False):
        super().__init__(msg)
        self.needs_human = needs_human


def _need_human(msg: str) -> None:
    if msg not in NEEDS_HUMAN:
        NEEDS_HUMAN.append(msg)
    print(f"[interpret] ⚠️ 사람 조치 필요: {msg}")

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


def total_changes(raw: dict) -> int:
    return sum(
        f.get("count", 0) for f in raw.get("files_changed", [])
        if isinstance(f.get("count"), int)
    )


def build_prompt(raw: dict, patch_notes: list[dict]) -> str:
    files = ", ".join(
        f"{f['path']} {f['count']}건" for f in raw.get("files_changed", [])
    ) or "미상"
    total = total_changes(raw)
    rt = (raw.get("raw_text") or "")
    # 와이프급 대형 패치는 본문이 수 MB — 앞부분만 보고 "사소한 변경"으로 오판하지 않도록
    # 총 변경 규모와 잘림 사실을 명시한다.
    raw_block = rt[:12000]
    if len(rt) > 12000:
        raw_block += (
            f"\n...[본문이 매우 커서 앞부분만 제공됨 — 실제 변경은 총 {total}건. "
            "규모(severity) 판단은 이 총계를 기준으로 할 것]"
        )
    return USER_TEMPLATE.format(
        eft_version=raw.get("eft_version") or "미상",
        posted_at=raw.get("posted_at") or "미상",
        files=f"{files} (총 {total}건)",
        raw_text=raw_block,
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


# ---------- Groq: 모델 자동 선택 + 오류 분류 ----------
def _groq_http(method: str, path: str, key: str, body: dict | None = None,
               timeout: int = 120) -> tuple[int | None, dict | None, str]:
    """Groq REST 호출. (상태코드, JSON 본문, 원문)을 돌려준다 — 네트워크 오류는 상태코드 None.

    예외 대신 상태코드와 오류 본문을 넘겨, 호출부가 인증/모델/파라미터/일시 오류를 구분한다.
    """
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        GROQ_BASE + path,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None,
        method=method,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status, text = resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            text = e.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            text = ""
        status = e.code
    except Exception as e:  # noqa: BLE001  (타임아웃·DNS·연결 끊김)
        return None, None, str(e)
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    return status, (data if isinstance(data, dict) else None), text


def _err_brief(status: int | None, data: dict | None, text: str) -> tuple[str, str]:
    """오류 응답에서 (code, 한 줄 요약)을 뽑는다. 로그·알림용이라 길이를 자른다."""
    err = (data or {}).get("error") if isinstance(data, dict) else None
    err = err if isinstance(err, dict) else {}
    code = str(err.get("code") or err.get("type") or "")
    msg = str(err.get("message") or text or "").replace("\n", " ").strip()[:200]
    return code, f"HTTP {status if status is not None else '연결실패'} {code} {msg}".strip()


def _classify(status: int | None, data: dict | None, text: str,
              params: dict) -> tuple[str, str, str | None]:
    """실패 응답을 (종류, 요약, 거부된 파라미터)로 분류한다.

    종류: transient(재시도) / auth(사람 조치) / model(다음 후보로) / param(해당 파라미터 빼고 재시도)
          / parse(응답 JSON 깨짐, 재시도) / other
    """
    code, brief = _err_brief(status, data, text)
    err = (data or {}).get("error") if isinstance(data, dict) else None
    err = err if isinstance(err, dict) else {}
    low = str(err.get("message") or text or "").lower()
    if status is None or status == 429 or status >= 500:
        return "transient", brief, None
    if status == 401:
        return "auth", brief, None
    if code == "json_validate_failed":        # json 모드에서 모델이 JSON 을 못 만든 경우
        return "parse", brief, None
    if status == 404 or code.startswith("model_"):   # model_not_found / model_decommissioned 등
        return "model", brief, None
    if status in (400, 422):
        for p in params:                      # 모델별로 지원 안 하는 선택 파라미터
            if err.get("param") == p or p in low:
                return "param", brief, p
    if status == 413:                         # 모델별 요청 크기/TPM 한도 — 다른 모델은 될 수 있음
        return "model", brief, None
    if status in (400, 403) and "model" in low:
        return "model", brief, None
    if status == 403:                         # 권한 거부·Cloudflare 차단 — 키/설정 확인 필요
        return "auth", brief, None
    return "other", brief, None


def _family_rank(model: str) -> int:
    m = model.lower()
    return next((i for i, f in enumerate(GROQ_FAMILIES) if f in m), len(GROQ_FAMILIES))


def _size_b(model: str) -> float:
    """모델명에서 파라미터 규모(예: 120b, 3.8b) 추출 — 같은 계열 안에서 큰 모델 우선."""
    sizes = re.findall(r"(\d+(?:\.\d+)?)b(?![a-z])", model.lower())
    return max((float(s) for s in sizes), default=0.0)


def _groq_candidates(key: str) -> list[str]:
    """활성 모델 목록을 조회해 시도 순서를 만든다(런당 1회)."""
    override = os.environ.get("GROQ_MODEL") or os.environ.get("LLM_MODEL")
    status, data, text = _groq_http("GET", "/models", key, timeout=30)
    active: list[str] | None = None
    if status == 200 and data and isinstance(data.get("data"), list):
        active = [
            m["id"] for m in data["data"]
            if isinstance(m, dict) and m.get("id") and m.get("active", True) is not False
        ]
    else:
        print(f"[interpret] 모델 목록 조회 실패 — 선호 목록으로 시도: {_err_brief(status, data, text)[1]}")

    if active is None:
        cands = list(GROQ_PREFERRED)
    else:
        usable = [m for m in active if not any(x in m.lower() for x in GROQ_EXCLUDE)]
        pref = [m for m in GROQ_PREFERRED if m in usable]
        rest = sorted(
            (m for m in usable if m not in pref and _family_rank(m) < len(GROQ_FAMILIES)),
            key=lambda m: (_family_rank(m), -_size_b(m), m),
        )
        cands = pref + rest
    if override:
        if active is not None and override not in active:
            print(f"[interpret] 지정 모델 {override} 이 활성 목록에 없음 — 자동 선택으로 대체")
        else:
            cands = [override] + [m for m in cands if m != override]
    cands = cands[:MAX_CANDIDATES]
    print(f"[interpret] 모델 후보: {', '.join(cands) or '(없음)'}")
    return cands


def _groq_params(model: str) -> dict:
    """모델별 선택 파라미터. 모델이 거부하면 _try_groq_model 이 그 항목을 빼고 재시도한다."""
    params: dict = {"temperature": TEMPERATURE, "response_format": {"type": "json_object"}}
    m = model.lower()
    if "gpt-oss" in m:
        params["reasoning_effort"] = "low"    # 추론 모델 — 해석엔 낮은 추론으로 충분, 토큰·지연 절약
    elif "qwen3" in m:
        params["reasoning_effort"] = "none"   # 사고 과정(<think>)이 본문에 섞이지 않게
    return params


def _try_groq_model(key: str, model: str, system: str,
                    user: str) -> tuple[str, str, dict | None]:
    """한 모델로 해석을 시도한다. 성공이면 ("", "", 결과), 실패면 (종류, 요약, None).

    - 거부된 선택 파라미터는 빼고 재시도(파라미터마다 1회)
    - 429/5xx/네트워크 오류는 RETRY_WAITS 대로 기다렸다 재시도
    - 응답 JSON 이 깨졌거나 스키마가 틀리면 1회 재시도
    """
    params = _GROQ["params"].setdefault(model, _groq_params(model))
    transient = parse_fail = 0
    while True:
        if time.monotonic() - _GROQ["started"] > LLM_BUDGET_SEC:
            return "budget", f"LLM 시간 예산({LLM_BUDGET_SEC}초) 초과", None
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            **params,
        }
        status, data, text = _groq_http("POST", "/chat/completions", key, body)
        if status == 200:
            try:
                content = (data or {})["choices"][0]["message"]["content"]
                return "", "", _validate(_extract_json(content))
            except Exception as exc:  # noqa: BLE001
                kind, brief = "parse", f"응답 JSON 해석 실패: {exc}"
        else:
            kind, brief, param = _classify(status, data, text, params)
            if kind == "param":
                print(f"[interpret] {model}: '{param}' 파라미터 거부 — 빼고 재시도 ({brief})")
                params.pop(param, None)
                continue
        if kind == "parse" and parse_fail < 1:
            parse_fail += 1
            print(f"[interpret] {model}: {brief} — 재시도")
            continue
        if kind == "transient" and transient < len(RETRY_WAITS):
            wait = RETRY_WAITS[transient]
            transient += 1
            print(f"[interpret] {model}: 일시 오류({brief}) — {wait}초 후 재시도")
            time.sleep(wait)
            continue
        print(f"[interpret] {model}: 실패[{kind}] {brief}")
        return kind, brief, None


def _interpret_groq(system: str, user: str) -> tuple[dict, str]:
    """후보 모델을 순서대로 시도해 (결과, 사용 모델)을 돌려준다. 성공한 모델은 런 동안 고정."""
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        msg = "GROQ_API_KEY 가 비어 있음 — 저장소 Secrets 에 Groq API 키를 등록하세요"
        _need_human(msg)
        raise LLMError(msg, needs_human=True)
    st = _GROQ
    if st["gave_up"]:
        # 같은 런에서 이미 모든 후보가 실패 — 남은 항목은 바로 보류(다음 실행에서 재시도)
        raise LLMError(f"이번 실행에서 LLM 해석 중단됨: {st['gave_up']}")
    if st["started"] is None:
        st["started"] = time.monotonic()
    if st["candidates"] is None:
        st["candidates"] = _groq_candidates(key)
    order = [m for m in dict.fromkeys(([st["model"]] if st["model"] else []) + st["candidates"])
             if m not in st["dead"]]

    kinds: list[str] = []
    last = "시도할 모델 없음"
    for model in order:
        kind, brief, result = _try_groq_model(key, model, system, user)
        if result is not None:
            if st["model"] != model:
                print(f"[interpret] 사용 모델: {model} (이번 실행 동안 고정)")
            st["model"] = model
            return result, model
        last = f"{model}: {brief}"
        if kind == "auth":   # 키는 모든 모델 공통 — 다른 후보를 시도해도 소용없다
            msg = f"Groq 인증 실패/접근 거부 — GROQ_API_KEY 재발급·재등록 필요 ({brief})"
            _need_human(msg)
            st["gave_up"] = msg
            raise LLMError(msg, needs_human=True)
        if kind == "budget":   # 시간 초과는 일시 문제로 보고 다음 실행에 맡긴다
            st["gave_up"] = brief
            raise LLMError(brief)
        if kind == "model":
            st["dead"].add(model)
            if st["model"] == model:
                st["model"] = None
        kinds.append(kind)

    if all(k in ("model", "other") for k in kinds):   # 후보가 없거나 전부 폐지·거부
        msg = (f"쓸 수 있는 Groq 모델이 없음(시도: {', '.join(order) or '없음'} / 마지막 {last}) "
               "— Variables 에 GROQ_MODEL 지정 또는 GROQ_PREFERRED 갱신 필요")
        _need_human(msg)
        st["gave_up"] = msg
        raise LLMError(msg, needs_human=True)
    st["gave_up"] = f"일시 오류로 후보 모두 실패(마지막 {last})"
    raise LLMError(st["gave_up"])


def _interpret_paid(provider: str, system: str, user: str) -> tuple[dict, str]:
    """유료 제공자(anthropic/openai) 경로 — 단일 모델, 3회 재시도."""
    env = "ANTHROPIC_API_KEY" if provider == "anthropic" else "OPENAI_API_KEY"
    if not os.environ.get(env):
        msg = f"LLM_PROVIDER={provider} 인데 {env} 가 비어 있음"
        _need_human(msg)
        raise LLMError(msg, needs_human=True)
    model = os.environ.get("LLM_MODEL") or DEFAULT_MODELS[provider]
    call = _call_openai if provider == "openai" else _call_anthropic
    # 429/일시 5xx나 비-JSON 응답 한 번에 런 전체가 죽지 않도록 재시도
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            return _validate(_extract_json(call(system, user, model))), f"{provider}:{model}"
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < 2:
                print(f"[interpret] LLM 호출/파싱 실패(시도 {attempt + 1}/3): {exc} — 15초 후 재시도")
                time.sleep(15)
    raise LLMError(f"LLM 해석 3회 실패: {last_exc}") from last_exc


def _stub(raw: dict) -> dict:
    files = raw.get("files_changed") or [{"path": "unknown"}]
    return {
        "summary_ko": "(스텁 모드) 자동 해석 대기 중 — 원문 diff 만 표시합니다.",
        "tags": ["기타"],
        "severity": "minor",
        "changes": [
            {
                "key_path": f.get("path", "unknown"),
                "before_ko": "-",
                "after_ko": "-",
                "explanation_ko": "API 키(GROQ_API_KEY)가 설정되면 이 항목이 한글로 자동 해석됩니다.",
                "impact_ko": "-",
            }
            for f in files
        ],
        "patch_note": {
            "matched": False,
            "title": None,
            "url": None,
            "reason_ko": "스텁 모드이므로 매칭을 수행하지 않았습니다.",
        },
    }


SEVERITIES = ("major", "minor", "trivial")
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _validate(result) -> dict:
    """LLM 응답 스키마 최소 검증 — 핵심 필드가 없으면 예외(호출부가 재시도), 부가 필드는 보정."""
    if not isinstance(result, dict):
        raise ValueError("JSON 객체가 아님")
    if not str(result.get("summary_ko") or "").strip():
        raise ValueError("summary_ko 없음")
    if not isinstance(result.get("changes"), list):
        raise ValueError("changes 목록 없음")
    result["changes"] = [c for c in result["changes"] if isinstance(c, dict)]
    if result.get("severity") not in SEVERITIES:
        result["severity"] = "minor"
    if not isinstance(result.get("tags"), list):
        result["tags"] = ["기타"]
    if not isinstance(result.get("patch_note"), dict):
        result["patch_note"] = {"matched": False, "title": None, "url": None,
                                "reason_ko": "매칭 결과가 없어 잠수함 패치로 처리했습니다."}
    return result


def _unverified_numbers(result: dict, raw_text: str | None) -> list[str]:
    """before/after 설명에 나온 수치 중 원문 diff 에 없는 것(환각 의심)을 찾는다.

    한 자리 수는 어디에나 있어 판별력이 없으므로 제외하고, 배율↔퍼센트(0.5↔50) 변환은 허용.
    결과는 로그와 항목의 unverified_numbers 에 남기기만 한다(해석을 버리지는 않음).
    """
    have: set[float] = set()
    for n in _NUM_RE.findall(raw_text or ""):
        try:
            have.add(float(n))
        except ValueError:
            pass
    missing: list[str] = []
    for c in result.get("changes") or []:
        for field in ("before_ko", "after_ko"):
            for n in _NUM_RE.findall(str(c.get(field) or "")):
                if len(n.lstrip("-").replace(".", "")) < 2 or n in missing:
                    continue
                v = float(n)
                if v in have or round(v / 100, 6) in have or round(v * 100, 6) in have:
                    continue
                missing.append(n)
    return missing[:10]


def _extract_json(text: str) -> dict:
    text = (text or "").strip()
    # 일부 추론 모델은 <think>…</think> 사고 과정을 본문에 붙인다 — 중괄호가 섞일 수 있어 먼저 제거
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1:
        text = text[start:end + 1]
    return json.loads(text)


def interpret(raw: dict, patch_notes: list[dict] | None = None) -> dict:
    patch_notes = patch_notes or []
    # 기본 provider: groq. 키가 없으면 스텁으로 조용히 넘어가지 않고 LLMError(사람 조치 필요) —
    # 스텁 결과는 '해석 완료'로 저장돼 다시 시도되지 않기 때문. 스텁은 LLM_PROVIDER=stub 로만.
    provider = (os.environ.get("LLM_PROVIDER") or "groq").lower()
    if provider not in ("groq", "anthropic", "openai", "stub"):
        print(f"[interpret] 알 수 없는(또는 폐지된) provider '{provider}' — groq 로 대체")
        provider = "groq"

    if provider == "stub":
        result, used = _stub(raw), "stub"
    else:
        prompt = build_prompt(raw, patch_notes)
        if provider == "groq":
            result, model = _interpret_groq(SYSTEM_PROMPT, prompt)
            used = f"groq:{model}"
        else:
            result, used = _interpret_paid(provider, SYSTEM_PROMPT, prompt)
    result["interpreter"] = used  # 어떤 모델이 해석했는지 항목에 남긴다(모델 교체 추적용)

    unverified = _unverified_numbers(result, raw.get("raw_text"))
    if unverified:
        result["unverified_numbers"] = unverified
        print(f"[interpret] 검증 경고: 원문에 없는 수치 {unverified} — 해석 확인 권장")

    pn = result.get("patch_note") or {}
    result["is_submarine"] = not bool(pn.get("matched"))

    # 대형 패치(와이프 등)를 앞 12KB만 보고 minor로 오판하는 것을 코드에서 교정
    total = total_changes(raw)
    if total >= 500 and result.get("severity") != "major":
        result["severity"] = "major"
        result["severity_note_ko"] = f"변경 총 {total}건 — 규모 기준 자동 상향"

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
