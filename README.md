# 타르코프 잠수함 패치 한글 해석 🛠️

[에스케이프 프롬 타르코프(EFT)](https://www.escapefromtarkov.com/)의 **사일런트(잠수함) 코드 변경**을
매일 자동으로 한글 번역·해석하는 정적 웹 서비스입니다.
원본 데이터는 [Tarkov Silent Changes](https://changes.tarkov-changes.com/)에서 가져오며,
공식 패치노트와 연결되면 링크를, 연결되지 않으면 **"잠수함 패치"**로 표시합니다.

> 비공식 팬 프로젝트입니다. 해석은 LLM 자동 생성이라 오류가 있을 수 있습니다.

## 동작 방식

```
매일 (GitHub Actions cron)
  └─ pipeline.py
       1) scrape      changes.tarkov-changes.com/latest 최신 변경 수집
       2) patchnotes  공식 패치노트 후보 목록 확보 (수동 + 자동)
       3) interpret   LLM 으로 한글 해석 + 패치노트 매칭 → 잠수함 패치 판별
       4) stability   전체 이력과 대조해 안정성 자동 판정(아래 참고)
       5) store       data/entries.json 에 신규 항목만 누적
       6) build       docs/data.json 생성 (사이트가 읽는 피드)
  └─ 변경분을 커밋 → GitHub Pages 가 docs/ 를 정적 호스팅
```

정적 사이트(`docs/`)는 빌드 단계 없이 `data.json` 한 파일만 읽어 렌더링합니다.

## 폴더 구조

```
.
├── pipeline.py                # 일일 파이프라인 오케스트레이터(+ 안정성 자동 판정)
├── scripts/
│   ├── scrape.py              # 사이트 파싱
│   ├── patchnotes.py          # 공식 패치노트 수집
│   ├── interpret.py           # LLM 해석 + 매칭
│   ├── stability.py           # diff 파싱 + 안정성 판정(토글/재변경 탐지)
│   ├── backfill_harvest.py    # 과거 /view/{id} 이력 수집(원본)
│   ├── backfill_apply.py      # 과거 해석(backfill_interp.json) 결합·반영
│   └── recompute_stability.py # 기존 전체 항목 안정성 재계산
├── data/
│   ├── entries.json           # 누적 해석 데이터(원본 저장소)
│   ├── history_raw.json       # 과거 이력 raw(안정성 재발 탐지 기반)
│   ├── backfill_interp.json   # 과거 항목 한글 해석(수기 큐레이션)
│   └── patchnotes_manual.json # 직접 추가하는 공식 패치노트(선택)
├── docs/                      # ← GitHub Pages 루트
│   ├── index.html / style.css / app.js
│   └── data.json              # 빌드 산출물(사이트 피드)
├── .github/workflows/daily.yml
└── requirements.txt
```

## 배포 (GitHub)

1. **이 폴더를 GitHub 저장소로 푸시**합니다.

2. **GitHub Pages 켜기**
   `Settings → Pages → Build and deployment`
   - Source: **Deploy from a branch**
   - Branch: **main** / 폴더 **`/docs`** 선택 → Save
   - 잠시 후 `https://<사용자명>.github.io/<저장소명>/` 에서 확인.

3. **LLM 설정 — 기본은 GitHub Models(무료, 키 불필요)** 🎉
   별도 설정 없이 바로 동작합니다. 워크플로가 GitHub Actions 가 자동 제공하는
   `GITHUB_TOKEN` 으로 [GitHub Models](https://docs.github.com/en/github-models)를
   호출하기 때문에 **API 키 등록도, 비용도 없습니다.** (하루 1회 호출이라 무료 한도로 충분)

   - 모델만 바꾸고 싶다면 `Settings → Secrets and variables → Actions → Variables`:
     - `LLM_MODEL` = 예) `openai/gpt-4o`, `openai/gpt-4o-mini`, `meta/Llama-3.3-70B-Instruct`
     - `PATCHNOTES_URL` = 공식 패치노트 페이지 URL(기본: EFT 공식 뉴스)

   - **유료 제공자로 전환**하고 싶을 때만(선택):
     - **Variables** → `LLM_PROVIDER` = `anthropic` 또는 `openai`
     - **Secrets** → `ANTHROPIC_API_KEY` 또는 `OPENAI_API_KEY` 등록

4. **워크플로 권한 확인**
   `Settings → Actions → General → Workflow permissions` →
   **Read and write permissions** 켜기 (봇이 결과를 커밋하려면 필요).

5. **첫 실행**
   `Actions` 탭 → "매일 타르코프 변경 해석" → **Run workflow** 로 수동 실행.
   이후 매일 자동 실행됩니다(기본 한국시간 23시, `daily.yml` 의 cron 으로 조정).

## 로컬에서 실행/테스트

```bash
pip install -r requirements.txt

# 1) GitHub Models 로 자동 해석 (무료) — GitHub 토큰만 있으면 됨
export GITHUB_TOKEN=ghp_...               # models:read 권한이 있는 토큰
python pipeline.py

# 1-b) 유료 제공자로 해석
LLM_PROVIDER=anthropic ANTHROPIC_API_KEY=sk-ant-... python pipeline.py

# 2) 키 없이 구조만 확인 (스텁 모드 — 원문 diff 만 표시)
LLM_PROVIDER=stub python pipeline.py

# 3) 사이트 미리보기
cd docs && python -m http.server 8000
#  → http://localhost:8000
```

## 커스터마이즈 포인트

- **해석 품질/말투**: `scripts/interpret.py` 의 `SYSTEM_PROMPT` 와 `USER_TEMPLATE` 수정.
- **패치노트 매칭 정확도**: `data/patchnotes_manual.json` 에 공식 공지를 직접 추가하면
  LLM 매칭 후보로 우선 사용됩니다(자동 수집이 막힐 때의 안전장치).
- **노출 개수**: `pipeline.py` 의 `FEED_LIMIT`.
- **실행 시각**: `.github/workflows/daily.yml` 의 cron.

## 안정성 자동 판정

각 변경의 raw diff 를 `(파일, 키 경로, 이전값, 새값)` 단위로 파싱해, 전체 이력에서
같은 키가 이후에 어떻게 바뀌는지를 보고 자동으로 분류합니다(`scripts/stability.py`).

- 📌 **stable(안정 유지)** — 이 변경 이후 동일 키가 다시 바뀐 적 없음(현재까지 유지로 추정).
- ♻️ **superseded(이후 갱신됨)** — 이후 같은 키가 다시 바뀜(되돌이는 아님). 값이 더 갱신된 상태.
- 🔁 **recurring(반복 이벤트)** — 같은 키 값이 이전 상태로 되돌아오는 토글이 과반.
  주말 경험치 부스트·에어드랍 이벤트처럼 **켰다 껐다 반복되는 변경**을 자동으로 잡아냅니다.

판정은 매일 파이프라인에서 자동 수행되며, 로직/이력이 바뀌면 다음으로 전체 재계산:

```bash
python scripts/recompute_stability.py
```

## 과거 이력 백필(선택)

`/list` 와 `/view/{id}` 로 공개된 과거 사일런트 변경을 수집해 한글 해석을 채울 수 있습니다.

```bash
# 1) 과거 원본 수집(요청 간 지연으로 정중하게)
python scripts/backfill_harvest.py --start <최신id> --count 120 --delay 0.7
# 2) data/backfill_interp.json 에 한글 해석 작성 후 결합·반영
python scripts/backfill_apply.py
```

## 한계와 주의

- 비로그인 `/latest` 는 최신 1건이지만, `/list`·`/view/{id}` 로 과거 이력에도 접근할 수 있습니다
  (백필은 이를 이용). 일상 파이프라인은 매일 최신 변경을 누적하는 방식입니다.
- 사이트 구조가 바뀌면 `scripts/scrape.py` 의 파싱을 조정해야 할 수 있습니다.
- 기본 제공자(GitHub Models)는 무료입니다. 유료 제공자로 바꿀 때만 API 비용이 발생합니다.
- 원본 사이트와 BSG 의 약관·robots 정책을 존중하세요. 과도한 요청을 피하기 위해 하루 1회만 호출합니다.
```
