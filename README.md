# youtube-comment-moderator

유튜브 채널 관리자를 위한 유해댓글 검토 도구. 채널을 연결하면 서버가 매시간
새 댓글을 모아 유해성을 판별하고, 관리자가 우선순위대로 확인해 조치하면
유튜브에 실제로 반영된다.

광주 인공지능사관학교 기업연계 프로젝트 (팀 Outlier).

---

## 무엇을 하는가

채널에 달린 댓글은 사람이 다 읽을 수 없을 만큼 많다. 그렇다고 자동으로
가려버리면 멀쩡한 댓글이 조용히 사라진다. 이 도구는 **판별은 기계가 하고
조치는 사람이 하도록** 나눈다.

```
로그인 (이메일+비밀번호, 또는 Google) → 채널 연결 (채널 주인의 YouTube OAuth) → AI 판별 동의
   ↓
서버가 매시간                            app/services/watcher.py
   최신 영상 댓글 수집 (새 것만)
   ① 관리자 등록어         차단어 → 숨김 (LLM 안 부름) / 검토어 → 표시
   ② ML 필터               확실히 정상인 댓글은 LLM 없이 통과 (PREFILTER_MODE=on)
   ③ LLM 판별              harmful / ambiguous / safe + 카테고리 + 위험도 1~5
   ④ 검토 큐               위험도순. 유해 4단계 이상은 증거 보관함에 원문 고정
   ↓
관리자가 하나씩             숨김 / 유지 / 채널차단
   ↓
유튜브에 반영               comments.setModerationStatus
```

등록어에 안 걸린 댓글은 ML 필터가 확실히 정상이라고 본 것만 빼고 전부 LLM으로 보낸다.
등록어만 검사하면 관리자가 미리 상상한 단어만 잡히고, 새 은어나 우회 표기는 그대로
빠져나가기 때문이다.

ML 필터는 공개 한국어 혐오표현 데이터(BEEP!, K-MHaS) 85,266건으로 학습한 글자 n-gram +
로지스틱 회귀다. 개발자 라벨 200건에서 문턱 0.083 일 때 LLM 호출 80.5%, 사람이 봐야 할
댓글 60건 중 놓침 +1. 모델 파일(`ml/`)은 레포에 없다 — `python -m scripts.train_prefilter`
로 만들고, 없으면 이 단계는 저절로 빠진다.

전 구간을 실제 채널로 확인했다 (2026-09-15): 숨김 → 시청자 조회에서 사라짐.

**한 번 가린 댓글은 API 로 되돌릴 수 없다** (2026-09-16 확인).
`setModerationStatus(published)` 는 204 를 돌려주지만 댓글은 그대로 가려져 있고,
유튜브 API 는 rejected 댓글을 읽는 방법조차 주지 않는다. 그래서 화면의
'큐로 되돌리기' 는 우리 기록만 되돌리고, 유튜브 쪽은 읽어서 확인된 경우에만
반영됨으로 표시한다. 유튜브에서 다시 공개하려면 스튜디오에서 직접 해야 한다.
숨김이 신중해야 하는 이유이자, 자동 숨김을 두지 않는 또 하나의 이유다.

## 설계에서 정한 것들

측정해보고 정한 것이라 되돌리려면 근거가 필요하다.

**자동 숨김을 두지 않는다.** 기본값은 전부 꺼짐. 마지막까지 '욕설' 하나는
남겨뒀었지만 실데이터에서 무너졌다 — 방송 고지문 인용, 행동 지적, `보지마`를
성기로 읽은 오독까지 사람을 안 거치고 가려졌다. 채널이 카테고리별로 직접 켤
수는 있다 (관리 기준 화면). 자세한 사례는
[`app/services/pipeline.py`](app/services/pipeline.py) 주석에 있다.

**확신 없으면 ambiguous.** LLM 이 harmful 이나 safe 로 억지로 정하지 않고
관리자에게 넘긴다. 검토 큐가 그만큼 길어지는데, 그게 이 제품의 방향이다 —
관리자가 안 본 채 남는 댓글이 없어야 한다.

**채널별 차이는 프롬프트가 아니라 정책으로.** 채널마다 다른 기준을 LLM 에
글로 써넣는 방식은 실험 결과 일관되지 않았다 (관리자 결정 30건에서 규칙을
뽑게 하면 사람이 쓴 것과 63% 일치, 3건에서 한 문장씩 뽑으면 90%). 지금은
채널별 자동 숨김 토글로 다루고, 자유 텍스트 기준은 참고 정보로만 붙는다.

**1차 규칙에 기본 차단 목록이 없다.** 1차는 판별기가 아니라 관리자 통제
장치다. 등록어가 하나도 없어도 파이프라인은 정상 동작한다.

**외부 유해표현 API 는 쓰지 않는다.** COREPIN 은 무료 규칙 대비 탐지율 차이가
1.4%p 인데 건당 5원이었고, OpenAI Moderation 은 한국어 탐지율이 19.8%였다.

**LLM 이 주는 confidence 는 쓰지 않는다.** 맞을 때 0.98, 틀릴 때 0.96 으로
구분이 되지 않았다.

## 프롬프트

[`app/services/llm.py`](app/services/llm.py). 판단 순서 4단계 + 카테고리 11개
+ 예시 23개. 실제로 LLM 이 받는 형태는 `python -m scripts.show_prompt` 로
본다 ([`docs/프롬프트_공통.txt`](docs/프롬프트_공통.txt) 에 스냅샷).

v3 까지는 새 댓글이 틀릴 때마다 그 댓글을 예시로 붙였다. 예시가 43개까지
늘었고 회귀 세트 39건 중 14건이 예시에 그대로 들어가 있었다 — 시험이 아니라
암기였다. v4 는 원칙을 네 질문으로 접고 예시를 대비쌍만 남겼다. 이력과
교훈은 파일 헤더에 있다.

**프롬프트 수정 규칙.** 새 오판 하나로 고치지 않는다.
[`eval/오류기록.csv`](eval/) 에 유형을 붙여 적고, 같은 유형이 3건 쌓이면
일반 원칙 한 줄을 고친다. 그 댓글을 예시로 넣지 않는다.

```
실데이터에서 오판 → 오류기록.csv 에 기록 (유형 포함)
   → 같은 유형 3건? → 예: 원칙 한 줄 수정 → 회귀 세트 전체 재실행
                    → 아니오: 기록만
```

## 회귀 세트

`eval/회귀세트.csv` 39건. 정답은 라벨이 아니라 '관리자가 봐야 하나' 로 매긴다
(통과 / 애매 / 유해). 프롬프트를 고치면 이걸로 채점한다:

```bash
python -m scripts.check_prompt              # 공통 프롬프트, 약 7원
python -m scripts.check_prompt --channel 4  # 채널 기준 붙여서
```

**이 점수는 정확도가 아니다.** 39건의 정답은 개발자가 매긴 것이고, 프롬프트가
우리가 박아둔 동작을 재현하는지 보는 회귀 시험이다. 판정은 같은 입력에도
약 8% 흔들리므로 ±2 안의 변화는 변화가 아니다. 실제 정확도는 사람이 라벨링한
데이터(`labeling/`, 4명 × 1,000건)가 나와야 말할 수 있다.

## 실행

운영은 네이버 클라우드 서버 한 대에서 컨테이너로 돈다 — [`deploy/README.md`](deploy/README.md).
아래는 노트북에서 개발용으로 띄우는 방법이다. PostgreSQL(pgvector) 과 Python 3.13 이 필요하다.

```bash
# 1. DB
docker run -d --name outlier-db \
  -e POSTGRES_PASSWORD=<비밀번호> -p 5432:5432 pgvector/pgvector:pg17

# 2. 설정
cp .env.example .env        # DB 비밀번호, YOUTUBE_API_KEY, OPENAI_API_KEY,
                            # GOOGLE_CLIENT_ID / SECRET (구글 콘솔 OAuth 클라이언트)
pip install -r requirements.txt
python -m scripts.init_db

# 3. 서버
python run.py               # 또는 python -m uvicorn app.main:app --port 8000
```

| 주소 | 화면 |
|---|---|
| http://localhost:8000/ | 로그인 · 가입 (이메일+비밀번호가 먼저, Google 은 아래 선택지) |
| http://localhost:8000/app | 관리자 화면 |
| http://localhost:8000/docs | API 문서 |
| http://localhost:8000/api/health/watch | 자동 감시 상태 (대시보드에도 나옴) |

`127.0.0.1` 이 아니라 `localhost` 로 접속한다. 구글 콘솔에 등록한 리디렉션
URI 와 같아야 한다. 개발 중 구글 없이 들어가려면 `.env` 에 `DEBUG=true`,
`DEV_LOGIN=true` 를 두고 `/api/auth/dev-login` 으로 간다.

## 자동 감시

서버가 켜져 있는 동안 연동 + 동의된 채널을 매시간 본다
([`app/services/watcher.py`](app/services/watcher.py)).

- 채널당 최신 영상 10개, 영상당 댓글 200건. 이미 있는 댓글은 건너뛰고
  새 것만 판별한다. 새 댓글이 0건이면 LLM 비용도 0이다
- 유튜브 쿼터는 채널당 하루 약 720 units. 채널 10개까지 들어온다
- 하루 LLM 상한 3,000건 (`LLM_DAILY_CAP`). 넘으면 그날은 멈추고 다음 날
  이어간다. 못 본 댓글은 pending 으로 남아 잃어버리지 않는다
- 주기·건수는 `.env` 의 `WATCH_*` 로 바꾼다

연동 안 된 채널(수집만 하는 샘플)은 감시 대상이 아니다. 그쪽은
`scripts/collect_channel` 과 `scripts/run_pipeline` 으로 따로 돌린다.

## API

`/docs` 에 전부 있다. 응답 예시는 [`docs/api/samples.json`](docs/api/samples.json)
(댓글 원문과 작성자명은 가려두었다).

| | |
|---|---|
| `GET /api/auth/start` | 로그인 시작 (구글 또는 개발용) |
| `GET /api/channels/connect/start` | 채널 연결 (YouTube 권한) |
| `GET /api/channels` | 채널 목록. `connected` 가 유튜브 권한 유무 |
| `GET·PUT /api/channels/{id}/consent` | AI 판별 동의 |
| `GET·PUT /api/channels/{id}/auto-hide` | 자동 숨김 카테고리 |
| `GET·PUT /api/channels/{id}/context` | 채널 기준 (참고 정보) |
| `POST /api/channels/{id}/disconnect` | 연동 해제 — 데이터 전부 삭제 |
| `POST·GET /api/channels/invites` | 채널 연결 초대 링크 (관리자가 만들어 유튜버에게 보냄) |
| `GET /connect/{token}` | 유튜버가 여는 초대 페이지 — 로그인 없이 채널 주인 계정으로 권한만 |
| `GET /api/channels/{id}/stats` | 처리 현황 |
| `GET /api/channels/{id}/queue` | 검토 큐 — 위험도순 |
| `GET /api/channels/{id}/hidden` | 숨김 목록 |
| `GET /api/channels/{id}/history` | 처리 이력 |
| `POST /api/comments/{id}/action` | 조치. 유튜브에 반영되고 `youtube_synced` 로 확인 |
| `GET /api/comments/{id}/similar` | 과거 유사 사례 |
| `GET·POST·PATCH·DELETE /api/channels/{id}/rules` | 등록어 |
| `POST /api/moderation/check` | 댓글 1건 즉시 판정 |

채널을 다루는 모든 엔드포인트는 `require_channel` 을 거친다. 남의 채널이면
404 다 (403 은 존재를 알려준다).

## 구조

```
app/
  api/        auth, channels, rules, moderation, review, health
  core/       config, deps (인증·격리의 유일한 관문)
  services/
    google_oauth.py    구글 로그인·채널 연동 (httpx 직접 구현)
    password.py        이메일 가입 비밀번호 해시 (scrypt, 표준 라이브러리)
    youtube_actions.py 숨김·복구·채널차단을 유튜브에 반영
    collector.py       댓글 수집. 기본 호출은 답글의 58%만 오므로 잘린 스레드 보충
    pattern.py         등록어 → 정규식. 기본은 적은 글자 그대로. 우회표현 확장(초성·끼어들기·늘이기)은 켤 수 있으나 기본 꺼짐
    moderation.py      등록어 검사 (block → review, 채널별 격리. 예외 동작은 뺐다)
    llm.py             LLM 판별. 프롬프트와 이력이 여기 있다
    pipeline.py        조각을 잇는다. 행선지 정책은 route() 하나에만
    store.py           판정 저장. 덮어쓰지 않고 쌓는다 (prompt_version 기록)
    watcher.py         자동 감시 (매시간)
    retention.py       30일 파기 (매일)
    embed.py           임베딩 (유사 사례)
  db/         모델·세션
  static/     관리자 화면 (빌드 없는 정적 파일)
scripts/      수집·판별·평가 CLI
tests/        pytest, 171개
```

## 주요 스크립트

```bash
python -m scripts.check_prompt                     # 회귀 세트 채점 (프롬프트 고친 뒤)
python -m scripts.show_prompt [채널id]             # LLM 이 받는 프롬프트 출력
python -m scripts.collect_channel @핸들 5 60       # 연동 안 된 채널 수집 (영상 5, 영상당 60)
python -m scripts.run_pipeline --channel 4 --save  # 판별 (--save 없으면 저장 안 함)
python -m scripts.consent --channel 4 --agree      # 샘플 채널 동의 플래그
python -m scripts.rejudge --channel 4 --dry        # 재판별 (건수·비용만)
python -m scripts.reroute --channel 4 --apply      # 정책만 바꿨을 때 재배치 (LLM 안 부름)
python -m scripts.set_password --email a@b.com     # 기존 계정에 비밀번호 붙이기
python -m pytest
```

## 데이터 취급

- 댓글 원문·작성자·임베딩·판단근거는 30일 뒤 서버가 자동으로 지운다.
  위험도 등 파생 지표만 남는다
- 채널마다 데이터를 격리한다. 다른 채널 지표를 한 화면에서 비교하지 않는다
- 채널 연동을 해제하면 그 채널의 댓글·판정·조치·영상·규칙을 전부 삭제한다
- `eval/`, `labeling/`, `.env` 는 레포에 올리지 않는다. `eval/회귀세트.csv` 와
  `eval/오류기록.csv` 는 실제 댓글이 들어 있어 팀 내부에서 따로 전달한다.
  없으면 `check_prompt` 가 파일 없다고 멈춘다

유튜브 정책상 댓글을 완전히 삭제할 수는 없다. 조치는
`comments.setModerationStatus(rejected)` 로 비공개 처리하는 것이고,
채널차단도 해당 채널에서만 적용된다. 유튜브가 보류(heldForReview)한 댓글은
OAuth 로 받아올 수 있다는 걸 확인했다.

## 지금 상태와 남은 것

되는 것: 로그인 → 연동 → 자동 수집·판별 → 검토 큐 → 조치 → 유튜브 반영 →
복구 → 이력 → 30일 파기. 실채널 1개 연동, 샘플 채널 3개.

정하지 않은 것:
- 위험도 표(`review.py` 의 `SEVERITY`)는 개발자가 정했고 큐의 65~80% 가
  '모욕=보통' 한 칸에 몰린다. LLM 이 직접 매기게 하려면 등급 정의를 팀이 써야 한다
- 채널별 기준을 누가 어떻게 만드는지. 실험 결과는 위 "설계에서 정한 것들" 에

## 평가 데이터

정확도 채점에는 [UnSmile](https://github.com/smilegate-ai/korean_unsmile_dataset)
을 쓴다. CC BY-NC-ND 라 채점 용도로만 쓰고, 단어를 추출해 규칙에 넣거나
상업적으로 이용하지 않는다. 레포에 포함하지 않는다.

## 팀

박승민(PM·백엔드) · 김지황(백엔드·DB) · 박정호(데이터 수집) ·
조민준(PM·발표) · 신효은(프론트엔드). 멘토 정연주.
