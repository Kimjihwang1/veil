"""채널 하나에 대한 도구들 — AI 프롬프트 · 지금 조회 · 자동 감지 설정.

화면(2026-11-03 디자인)의 'AI 프롬프트', 검토 피드의 [조회], '자동 감지' 가 부른다.
셋 다 require_channel 을 거친다. 남의 채널은 404 다.
"""

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.deps import require_channel
from app.db.models import Channel
from app.db.session import get_db
from app.services import watcher
from app.services.severity import level_of
from app.services.llm import CHANNEL_SLOT, LlmJudge, VideoContext, default_template, prompt_version

router = APIRouter(tags=["channel-tools"])



# ── AI 프롬프트 ────────────────────────────────────────

class PromptOut(BaseModel):
    channel_id: int
    template: str = Field(..., description=f"지금 쓰는 프롬프트. {CHANNEL_SLOT} 자리에 채널 판단 기준이 들어간다")
    default_template: str
    is_default: bool
    prompt_version: str = Field(..., description="채널 기준까지 넣은 최종 프롬프트의 지문")


class PromptIn(BaseModel):
    template: str | None = Field(
        None, max_length=20000, description="비우거나 기본값과 같으면 기본 프롬프트로 돌아간다"
    )


def _prompt_out(ch: Channel) -> PromptOut:
    return PromptOut(
        channel_id=ch.id,
        template=ch.prompt_template or default_template(),
        default_template=default_template(),
        is_default=ch.prompt_template is None,
        prompt_version=prompt_version(ch.context or "", ch.prompt_template),
    )


@router.get("/channels/{channel_id}/prompt", response_model=PromptOut, summary="판별 프롬프트")
async def get_prompt(channel: Channel = Depends(require_channel)):
    return _prompt_out(channel)


@router.put("/channels/{channel_id}/prompt", response_model=PromptOut, summary="판별 프롬프트 저장")
async def put_prompt(
    payload: PromptIn,
    channel: Channel = Depends(require_channel),
    db: AsyncSession = Depends(get_db),
):
    t = (payload.template or "").strip()
    # 기본값과 같으면 저장하지 않는다. 기본 프롬프트가 나중에 고쳐지면 따라가야 한다.
    channel.prompt_template = None if not t or t == default_template().strip() else t
    await db.commit()
    return _prompt_out(channel)


class TestIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)
    video_title: str | None = Field(None, max_length=200)
    template: str | None = Field(
        None, max_length=20000, description="저장 전 초안. 없으면 저장된 프롬프트로 시험한다"
    )


class TestOut(BaseModel):
    label: str | None
    category: str | None
    level: int | None = Field(None, description="검토 큐와 같은 규칙으로 매긴 위험도 1~5. 정상이면 null")
    confidence: float | None
    reason: str | None
    raw: str = Field("", description="모델이 돌려준 원문")
    error: str | None = None
    tests_left: int = Field(..., description="오늘 이 채널에서 더 시험할 수 있는 횟수")


# 채널마다 하루에 시험할 수 있는 횟수. 한 번에 0.17~0.31원이라 100번이면 30원 안쪽이다.
# 버튼 연타나 자동 스크립트로 비용이 튀는 걸 막는다. 서버가 재시작하면 다시 센다.
TEST_DAILY = 100
_시험: dict[tuple[int, str], int] = {}


def _오늘() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


@router.post("/channels/{channel_id}/prompt/test", response_model=TestOut, summary="댓글 하나 시험 판별")
async def test_prompt(payload: TestIn, channel: Channel = Depends(require_channel)):
    """GPT 를 한 번 부른다. 결과는 저장하지 않는다."""
    if not get_settings().openai_api_key:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "서버에 OpenAI 키가 없어 AI를 부를 수 없어요.")
    key = (channel.id, _오늘())
    used = _시험.get(key, 0)
    if used >= TEST_DAILY:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, f"오늘 시험 횟수({TEST_DAILY}번)를 다 썼어요. 내일 다시 해 주세요.")
    # 정기 감시와 같은 하루 상한을 나눠 쓴다. 시험이 감시 몫을 다 먹지는 못한다.
    if watcher._남은_오늘_호출() <= 0:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "오늘 AI 호출 상한에 도달했어요.")

    judge = LlmJudge(
        concurrency=1, max_calls=1, channel_context=channel.context or "",
        prompt_template=(payload.template or "").strip() or channel.prompt_template,
    )
    v = await judge.judge(payload.text, video=VideoContext(title=payload.video_title))
    _시험[key] = used + 1
    watcher.상태["llm_calls_today"] += judge.stats.calls

    lv = level_of(v.severity, v.category, v.label) if v.label and v.label != "safe" else None
    return TestOut(
        label=v.label or None, category=v.category or None, level=lv,
        confidence=v.confidence if v.label else None, reason=v.reason or None,
        raw=v.raw, error=v.error, tests_left=TEST_DAILY - _시험[key],
    )


# ── 지금 조회 ──────────────────────────────────────────

class FetchOut(BaseModel):
    collected: int = Field(..., description="새로 들어온 댓글")
    judged: int
    queued: int = Field(..., description="그중 검토 큐로 간 것")
    errors: list[str]


# 같은 채널을 너무 자주 누르면 유튜브 쿼터만 쓴다. 이 간격 안에서는 막는다.
# 60초였는데 시연 때 관객 댓글을 바로 보려고 5초로 줄였다 (2026-11-04).
FETCH_GAP = timedelta(seconds=5)


@router.post("/channels/{channel_id}/fetch", response_model=FetchOut, summary="지금 새 댓글 가져오기")
async def fetch_now(channel: Channel = Depends(require_channel)):
    if not channel.ai_consent_agreed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "AI 판별에 동의한 채널만 불러올 수 있어요.")
    last = channel.last_watched_at
    now = datetime.now(UTC).replace(tzinfo=None)
    if last and now - last < FETCH_GAP:
        left = int((FETCH_GAP - (now - last)).total_seconds()) + 1
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, f"방금 불러왔어요. {left}초 뒤에 다시 눌러 주세요.")
    try:
        r = await watcher.fetch_channel(channel.id)
    except watcher.Busy:
        raise HTTPException(status.HTTP_409_CONFLICT, "이 채널을 지금 불러오는 중이에요.")
    return FetchOut(collected=r["collected"], judged=r["judged"], queued=r["queued"], errors=r["errors"])


# ── 자동 감지 ──────────────────────────────────────────

class WatchOut(BaseModel):
    channel_id: int
    server_enabled: bool = Field(..., description="서버 전체 감시(WATCH_ENABLED). 꺼져 있으면 채널 설정과 무관하게 안 돈다")
    eligible: bool = Field(..., description="유튜브 권한 + AI 판별 동의. 둘 중 하나라도 없으면 정기 감시 대상이 아니다")
    enabled: bool
    interval_min: int
    last_run_at: datetime | None
    next_run_at: datetime | None
    llm_calls_today: int
    llm_daily_cap: int


class WatchIn(BaseModel):
    enabled: bool | None = None
    interval_min: int | None = Field(None, ge=5, le=1440, description="5분 ~ 24시간")


def _watch_out(ch: Channel) -> WatchOut:
    cfg = get_settings()
    mins = watcher.interval_min(ch)
    on = ch.watch_enabled is not False
    last = ch.last_watched_at
    nxt = None
    if on:
        nxt = (last + timedelta(minutes=mins)) if last else datetime.now(UTC).replace(tzinfo=None)
    watcher._남은_오늘_호출()  # 날이 바뀌었으면 오늘 쓴 양을 0 으로
    return WatchOut(
        channel_id=ch.id,
        server_enabled=cfg.watch_enabled,
        eligible=bool(ch.youtube_refresh_token) and bool(ch.ai_consent_agreed),
        enabled=on,
        interval_min=mins,
        last_run_at=last,
        next_run_at=nxt,
        llm_calls_today=watcher.상태["llm_calls_today"],
        llm_daily_cap=cfg.llm_daily_cap,
    )


@router.get("/channels/{channel_id}/watch", response_model=WatchOut, summary="자동 감지 설정")
async def get_watch(channel: Channel = Depends(require_channel)):
    return _watch_out(channel)


@router.put("/channels/{channel_id}/watch", response_model=WatchOut, summary="자동 감지 켜기·끄기·주기")
async def put_watch(
    payload: WatchIn,
    channel: Channel = Depends(require_channel),
    db: AsyncSession = Depends(get_db),
):
    if payload.enabled is not None:
        channel.watch_enabled = payload.enabled
    if payload.interval_min is not None:
        channel.watch_interval_min = payload.interval_min
    await db.commit()
    return _watch_out(channel)
