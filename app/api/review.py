"""검토 큐 · 숨김 목록 · 조치 · 통계 (F_R_112, F_R_115, F_R_116).

관리자가 실제로 쓰는 화면이 읽는 API 다. 설계 전제 두 가지.

  1. 관리자는 전부 못 본다. 그래서 큐는 위험도 순, 같은 위험도 안에서는
     확산도(좋아요+답글) 높은 순으로 준다 — 많이 퍼진 것부터 처리해야
     피해가 준다.

  2. 숨김 목록은 반드시 열어볼 수 있어야 한다. 가린 것을 되돌릴 통로가
     없으면 잘못 가린 게 영원히 안 보인다.
"""

from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import case, func, select
from sqlalchemy import text as sq
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import current_user, require_channel, require_comment
from app.db.models import Action, Channel, Comment, Evidence, RiskAssessment, User, Video
from app.db.session import get_db
from app.services import youtube_actions as yt
from app.services.collector import HELD
from app.services.severity import DEFAULT, FALLBACK

router = APIRouter(tags=["review"])

ActionType = Literal["hide", "keep", "ban_author"]

# 조치 -> 댓글이 가질 상태
ACTION_STATUS = {"hide": "hidden", "keep": "passed", "ban_author": "hidden"}

# 위험도는 1~5단계다 (2026-11-09). 정의와 대체 규칙은 services/severity.py.
#
# 전에는 카테고리 -> critical/high/medium/low 표였다. 실데이터에서 큐의 65% 가
# '모욕 = medium' 한 칸에 몰려서 순서가 거의 안 매겨졌다. 카테고리는 같아도
# 무게는 다른데("롤 개못하네 ㅋㅋ" 와 "가정교육 독학하신 티가 나네요" 가 같은 모욕이다)
# 표가 그 차이를 버렸다. 지금은 댓글을 읽은 LLM 이 매긴 값(risk_assessments.severity)을
# 쓰고, 그 값이 없는 판정만 표로 대신한다.


def _spread():
    return func.coalesce(Comment.like_count, 0) + func.coalesce(
        Comment.total_reply_count, 0
    )


def _level():
    """severity.level_of 의 SQL 판. 큐 정렬과 화면 표시가 같은 값을 쓴다.

    확산도로 등급을 올리는 규칙(SPREAD_BUMP=100)은 뺐다. 100 이라는 값에
    근거가 없었다 — 진용진 큐 865건 중 38건(4.4%)만 넘었고, 왜 4% 여야
    하는지 아무도 정한 적이 없다. 확산도는 같은 등급 안에서 순서를 가르는
    데는 계속 쓴다 (_rows 의 order_by).
    """
    by_category = case(FALLBACK, value=RiskAssessment.category, else_=DEFAULT)
    raw = case(
        (RiskAssessment.severity.between(1, 5), RiskAssessment.severity),
        else_=by_category,
    )
    return case(
        (RiskAssessment.risk_level == "ambiguous", func.greatest(raw - 1, 1)),
        else_=raw,
    )


class QueueItem(BaseModel):
    comment_id: int
    level: int = Field(
        ..., ge=1, le=5,
        description="위험도 1~5 (5 긴급). AI 가 매긴 값, 없으면 카테고리로. 애매 판정은 한 단계 내린다",
    )

    channel_title: str | None
    video_id: str | None
    video_title: str | None
    published_at: datetime | None = Field(None, description="유튜브에 달린 시각")

    content: str
    author_name: str | None
    like_count: int
    total_reply_count: int
    is_reply: bool
    parent_content: str | None = Field(None, description="답글이면 부모 댓글 본문")

    status: str
    destination: str | None = Field(None, description="queue_judge=판단 필요 / queue_info=참고")
    decided_by: str | None = Field(None, description="rule=관리자 등록어 / prefilter=앞단 규칙·ML / llm")
    label: str | None
    category: str | None
    reason: str | None
    rule_value: str | None = Field(None, description="등록어에 걸렸으면 그 단어")
    youtube_synced: bool | None = Field(
        None,
        description="마지막 조치가 유튜브에 실제로 반영됐는지. 조치 기록이 없으면 null. "
                    "숨김 목록에서 '되돌릴 수 없음' 과 '다시 보내기' 를 가르는 값",
    )
    youtube_held: str | None = Field(
        None,
        description="유튜브가 공개하지 않고 잡아 둔 댓글이면 heldForReview(보류) / "
                    "likelySpam(스팸 의심). 이 댓글을 [유지]하면 유튜브에 공개를 보낸다",
    )
    evidence: bool = Field(False, description="증거 보관함에 이 댓글이 있는지")


class ActionRequest(BaseModel):
    action: ActionType = Field(..., description="hide=숨김 / keep=유지 / ban_author=채널차단")
    note: str | None = Field(None, description="판단 근거 메모")


class ActionResult(BaseModel):
    comment_id: int
    action: ActionType
    status: str
    youtube_synced: bool = Field(
        False, description="유튜브에 실제로 반영됐는지. 채널 미연동이면 False"
    )
    note: str | None = Field(None, description="실패했으면 그 사유")
    reviewed_at: datetime


PERIOD_DAYS = {"today": 1, "7d": 7, "30d": 30, "all": None}


def _since(period: str):
    """기간 필터의 기준 시각. all 이면 None (필터 없음)."""
    days = PERIOD_DAYS.get(period)
    if days is None:
        return None
    return datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days)


class Workload(BaseModel):
    """관리자가 실제로 처리한 건수. 세는 값만 둔다.

    '건당 몇 초'와 '전부 보면 몇 시간'을 여기서 내보냈었다. 조치 기록 사이의
    시간 간격 중앙값이었는데, 그 기록은 개발 중에 우리가 누른 것이라 관리자
    판단 시간이 아니었다. 실제로 관리자는 대개 5초도 안 걸린다고 한다.
    잰 게 아닌 숫자를 '실측'이라고 화면에 띄우고 있었다. 뺐다.
    """

    reviewed: int = Field(..., description="관리자가 실제로 처리한 건수")
    seen_ratio: float = Field(..., description="전체 중 관리자가 본 비율")


class Stats(BaseModel):
    channel_id: int
    period: str
    total: int
    pending: int
    passed: int
    queued: int
    hidden: int
    unreviewed: int = Field(..., description="큐에 남아 관리자를 기다리는 건수")
    by_category: dict[str, int]
    workload: Workload


def _latest_assessment():
    """댓글별 '가장 최근' 판정만 고르는 서브쿼리.

    재판별하면 행이 쌓이므로 최신 것만 봐야 한다. id 가 순증이라 max(id) 로
    충분하다 (created_at 이 같은 초에 몰려도 안전하다).
    """
    return (
        select(
            RiskAssessment.comment_id.label("cid"),
            func.max(RiskAssessment.id).label("rid"),
        )
        .group_by(RiskAssessment.comment_id)
        .subquery()
    )


async def _rows(db, channel_id, where, limit, offset):
    latest = _latest_assessment()
    parent = Comment.__table__.alias("parent")
    level = _level()
    saved = select(Evidence.id).where(Evidence.comment_id == Comment.id).exists()
    # 마지막 조치의 유튜브 반영 여부. 숨김 목록에서 "가려짐·되돌릴 수 없음" 과
    # "유튜브 반영 실패 → 다시 보내기" 를 갈라야 해서 같이 가져온다.
    last_action = (
        select(Action.comment_id.label("cid"), func.max(Action.id).label("aid"))
        .group_by(Action.comment_id)
        .subquery()
    )

    stmt = (
        select(
            Comment,
            RiskAssessment,
            parent.c.content.label("parent_content"),
            Video.title.label("video_title"),
            Channel.channel_title.label("channel_title"),
            level.label("level"),
            Action.youtube_synced.label("youtube_synced"),
            saved.label("evidence"),
        )
        .join(latest, latest.c.cid == Comment.id)
        .join(RiskAssessment, RiskAssessment.id == latest.c.rid)
        .outerjoin(parent, parent.c.youtube_comment_id == Comment.parent_comment_id)
        .outerjoin(Video, Video.video_id == Comment.video_id)
        .outerjoin(Channel, Channel.id == Comment.channel_id)
        .outerjoin(last_action, last_action.c.cid == Comment.id)
        .outerjoin(Action, Action.id == last_action.c.aid)
        .where(Comment.channel_id == channel_id, *where)
        # 위험한 것부터, 같은 등급 안에서는 많이 퍼진 것부터.
        # 관리자 시간이 유한하다는 게 이 정렬의 이유다.
        .order_by(level.desc(), _spread().desc(), Comment.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return (await db.execute(stmt)).all()


def _to_item(c, ra, parent_content, video_title, channel_title, level, youtube_synced=None,
             evidence=False) -> QueueItem:
    return QueueItem(
        youtube_synced=youtube_synced,
        evidence=bool(evidence),
        youtube_held=c.youtube_moderation if c.youtube_moderation in HELD else None,
        comment_id=c.id,
        level=level,
        channel_title=channel_title,
        video_id=c.video_id,
        video_title=video_title,
        published_at=c.published_at,
        content=c.content,
        author_name=c.author_name,
        like_count=c.like_count or 0,
        total_reply_count=c.total_reply_count or 0,
        is_reply=bool(c.is_reply),
        parent_content=parent_content,
        status=c.status,
        destination=ra.destination,
        decided_by=ra.stage,
        label=ra.risk_level,
        category=ra.category,
        reason=ra.reasoning,
        rule_value=ra.rule_value,
    )


@router.get(
    "/channels/{channel_id}/queue",
    response_model=list[QueueItem],
    summary="검토 큐 (관리자가 판단해야 할 댓글)",
)
async def queue(
    channel: Channel = Depends(require_channel),
    kind: Literal["all", "judge", "info"] = Query(
        "all", description="judge=판단 필요만 / info=참고만"
    ),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    channel_id = channel.id
    where = [Comment.status == "queued", Comment.reviewed_at.is_(None)]
    if kind != "all":
        target = "queue_judge" if kind == "judge" else "queue_info"
        where.append(RiskAssessment.destination == target)

    rows = await _rows(db, channel_id, where, limit, offset)
    return [_to_item(*r) for r in rows]


@router.get(
    "/channels/{channel_id}/hidden",
    response_model=list[QueueItem],
    summary="숨김 목록 (오탐을 발견하는 통로)",
)
async def hidden(
    channel: Channel = Depends(require_channel),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    rows = await _rows(db, channel.id, [Comment.status == "hidden"], limit, offset)
    return [_to_item(*r) for r in rows]


@router.get(
    "/channels/{channel_id}/passed",
    response_model=list[QueueItem],
    summary="통과 목록 (AI 가 정상이라 한 것 — 놓침을 발견하는 통로)",
)
async def passed(
    channel: Channel = Depends(require_channel),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """AI 가 정상으로 판정해 관리자 앞에 안 온 댓글. 큐만 보면 '놓친 것' 은 영영 못 본다.
    관리자가 이 목록에서 [숨김] 을 누르면 그게 곧 AI 의 놓침 기록이 된다 (2026-09-30)."""
    rows = await _rows(db, channel.id, [Comment.status == "passed"], limit, offset)
    return [_to_item(*r) for r in rows]


@router.post(
    "/comments/{comment_id}/action",
    response_model=ActionResult,
    summary="조치 (숨김 / 유지 / 채널차단)",
)
async def act(
    payload: ActionRequest,
    comment: Comment = Depends(require_comment),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    comment_id = comment.id
    now = datetime.now(UTC).replace(tzinfo=None)

    # 이미 유튜브에서 가려진 걸 또 가리면 이력만 한 줄 더 생기고 유튜브에는 아무
    # 변화가 없다 → 409. 단, 마지막 숨김이 유튜브 반영에 실패한 것(토큰 만료·미연동)
    # 은 다시 보내야 한다. 그게 '유튜브에 다시 보내기' 다 (2026-09-30).
    if payload.action == "hide" and comment.status == "hidden":
        last = (
            await db.execute(
                select(Action.youtube_synced)
                .where(Action.comment_id == comment.id)
                .order_by(Action.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if last:
            raise HTTPException(status.HTTP_409_CONFLICT, "이미 유튜브에서 가려진 댓글입니다")

    # 숨겼던 걸 [복구(공개)] 한 것인지. 같은 keep 이라도 뜻이 다르다.
    #   검토 큐에서 누른 keep  = "봤는데 정상이다"      -> 판단 끝, 통과
    #   숨김 목록에서 누른 keep = "이건 숨기면 안 됐다"  -> 되돌리기
    # 되돌리기를 통과로 처리하면 그 댓글이 어느 화면에도 안 남는다.
    # 숨김 목록에서도 빠지고 검토 큐에도 없어서, 잘못 숨겼다가 되돌린
    # 댓글을 다시 볼 방법이 사라진다. 아직 판단하지 않은 상태로 돌린다.
    되돌림 = payload.action == "keep" and comment.status == "hidden"

    # 유튜브가 보류·스팸 의심으로 잡아 둔 댓글. 지금 유튜브에서 안 보이는 상태라
    # [유지] 는 "공개해도 된다" 는 뜻이 되고, 유튜브에 published 를 보내야 한다.
    보류 = comment.youtube_moderation in HELD

    # 유튜브에 실제로 반영한다. 채널 소유자가 연동하지 않았으면 토큰이
    # 없어서 DB 에만 기록된다 — youtube_synced 가 그 구분이다.
    #
    # 유튜브 반영이 실패해도 DB 기록은 남긴다. 관리자는 이미 판단을
    # 내렸고, 그 판단까지 없던 일로 만들면 같은 댓글을 또 보게 된다.
    channel = await db.get(Channel, comment.channel_id)
    동기화, 메모 = False, payload.note
    try:
        if payload.action == "hide":
            r = await yt.set_moderation(
                channel.youtube_refresh_token, [comment.youtube_comment_id], yt.HIDE
            )
        elif payload.action == "ban_author":
            r = await yt.ban_author(
                channel.youtube_refresh_token, comment.youtube_comment_id
            )
        elif 되돌림:  # keep — 숨겼던 것을 다시 공개해 본다
            r = await yt.set_moderation(
                channel.youtube_refresh_token,
                [comment.youtube_comment_id],
                yt.PUBLISH,
            )
            # 유튜브는 가린 댓글을 API 로 되돌리지 못한다. published 호출이 204 를
            # 돌려주고도 댓글은 그대로 가려져 있다 (실채널로 확인). 호출 성공을
            # 반영으로 치면 관리자는 공개된 줄 안다. 실제로 보이는지 읽어서 정한다.
            if r.ok and not await yt.is_published(
                channel.youtube_refresh_token, comment.youtube_comment_id
            ):
                r = yt.ActionResult(
                    False,
                    "유튜브는 API 로 가린 댓글을 되돌리지 못합니다. 우리 기록만 검토 큐로 "
                    "돌렸습니다. 유튜브에서 다시 공개하려면 YouTube 스튜디오에서 직접 해야 합니다.",
                )
        elif 보류:  # keep — 유튜브가 보류해 둔 것을 공개한다
            # 보류 댓글에 published 가 실제로 먹는지는 실채널로 아직 안 쟀다.
            # (안 먹던 건 우리가 rejected 로 가린 댓글이었다.) 204 를 반영으로 친다.
            r = await yt.set_moderation(
                channel.youtube_refresh_token,
                [comment.youtube_comment_id],
                yt.PUBLISH,
            )
        else:
            # 원래 공개돼 있던 걸 [유지] 한 것이라 유튜브에서 할 일이 없다.
            # youtube_synced 를 True 로 두면 안 된다 — '반영됐다'가 아니라
            # '반영할 게 없었다'이고, 나중에 이력에서 구분이 안 된다.
            # 그렇다고 '실패'도 아니라서 메모를 따로 붙이지 않는다.
            r = None

        if r is not None:
            동기화 = r.ok
            if not r.ok:
                메모 = f"{메모 or ''} [유튜브 반영 실패: {r.detail}]".strip()
            elif 보류:
                # 유튜브에서 더는 보류가 아니다. 표시를 지워야 피드의 '유튜브 보류' 가 사라진다.
                comment.youtube_moderation = yt.PUBLISH if payload.action == "keep" else yt.HIDE
    except yt.NotConnected as e:
        메모 = f"{메모 or ''} [미연동: {e}]".strip()
    except Exception as e:  # 유튜브가 죽어도 관리자 판단은 남긴다
        메모 = f"{메모 or ''} [유튜브 오류: {type(e).__name__}]".strip()

    db.add(
        Action(
            comment_id=comment_id,
            action_type=payload.action,
            # 조치자는 로그인한 사람이다. 요청 본문으로 받으면 남의 이름으로
            # 기록을 남길 수 있고, 이력이 증거 구실을 못 하게 된다.
            actor=user.email,
            note=메모,
            youtube_synced=동기화,
            executed_at=now,
        )
    )
    if 되돌림:
        # 검토 큐는 status=queued 와 reviewed_at IS NULL 둘 다 본다.
        # 하나만 되돌리면 어느 목록에도 안 뜬다.
        comment.status = "queued"
        comment.reviewed_at = None
    else:
        comment.status = ACTION_STATUS[payload.action]
        comment.reviewed_at = now
    await db.commit()

    return ActionResult(
        comment_id=comment_id,
        action=payload.action,
        status=comment.status,
        youtube_synced=동기화,
        note=메모,
        reviewed_at=now,
    )


@router.get(
    "/channels/{channel_id}/stats", response_model=Stats, summary="채널 처리 현황"
)
async def stats(
    channel: Channel = Depends(require_channel),
    period: Literal["today", "7d", "30d", "all"] = Query(
        "all", description="수집 시각 기준"
    ),
    db: AsyncSession = Depends(get_db),
):
    channel_id = channel.id
    since = _since(period)
    # 수집 시각 기준이다. 유튜브에 달린 시각이 아니라 우리가 가져온 시각 —
    # 오래된 영상을 오늘 수집하면 오늘 치로 잡히는 게 관리자 관점에 맞다.
    window = [Comment.collected_at >= since] if since else []

    counts = dict(
        (
            await db.execute(
                select(Comment.status, func.count())
                .where(Comment.channel_id == channel_id, *window)
                .group_by(Comment.status)
            )
        ).all()
    )
    total = sum(counts.values())

    unreviewed = (
        await db.execute(
            select(func.count())
            .select_from(Comment)
            .where(
                Comment.channel_id == channel_id,
                Comment.status == "queued",
                Comment.reviewed_at.is_(None),
                *window,
            )
        )
    ).scalar_one()

    latest = _latest_assessment()
    by_category = dict(
        (
            await db.execute(
                select(RiskAssessment.category, func.count())
                .join(latest, latest.c.rid == RiskAssessment.id)
                .join(Comment, Comment.id == RiskAssessment.comment_id)
                .where(
                    Comment.channel_id == channel_id,
                    RiskAssessment.category.isnot(None),
                    RiskAssessment.category != "정상",
                    *window,
                )
                .group_by(RiskAssessment.category)
                .order_by(func.count().desc())
            )
        ).all()
    )

    # 검토 전환율(큐 / 판별한 것, NF_R_104 목표 30%)을 내보냈었다. 기획서의
    # 개발 지표라 관리자가 보고 할 수 있는 일이 없어서 뺐다 (2026-11-03).
    return Stats(
        channel_id=channel_id,
        period=period,
        total=total,
        pending=counts.get("pending", 0),
        passed=counts.get("passed", 0),
        queued=counts.get("queued", 0),
        hidden=counts.get("hidden", 0),
        unreviewed=unreviewed,
        by_category=by_category,
        workload=await _workload(db, channel_id, total),
    )


async def _workload(db, channel_id: int, total: int) -> Workload:
    처리 = (
        await db.execute(
            sq("""
                SELECT count(*) FROM comments
                WHERE channel_id = :cid AND reviewed_at IS NOT NULL
            """),
            {"cid": channel_id},
        )
    ).scalar_one()
    return Workload(
        reviewed=처리,
        seen_ratio=round(처리 / total, 4) if total else 0.0,
    )


class HistoryItem(BaseModel):
    action_id: int
    comment_id: int
    action: str = Field(..., description="hide=숨김 / keep=유지 / ban_author=채널차단")
    actor: str | None
    note: str | None
    youtube_synced: bool
    executed_at: datetime | None

    content: str
    author_name: str | None
    category: str | None = Field(None, description="조치 당시 AI 가 본 분류")
    reason: str | None


@router.get(
    "/channels/{channel_id}/history",
    response_model=list[HistoryItem],
    summary="처리 이력 (누가 언제 무엇을 왜)",
)
async def history(
    channel: Channel = Depends(require_channel),
    action: Literal["all", "hide", "keep", "ban_author"] = Query("all"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """조치 이력을 최신순으로. 관리자가 자기 판단을 되짚어볼 수 있어야 하고,
    나중에 이 이력이 채널별 맥락을 만드는 재료가 된다 (F_R_116).
    """
    channel_id = channel.id
    latest = _latest_assessment()
    where = [] if action == "all" else [Action.action_type == action]

    rows = (
        await db.execute(
            select(Action, Comment, RiskAssessment)
            .join(Comment, Comment.id == Action.comment_id)
            .outerjoin(latest, latest.c.cid == Comment.id)
            .outerjoin(RiskAssessment, RiskAssessment.id == latest.c.rid)
            .where(Comment.channel_id == channel_id, *where)
            .order_by(Action.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()

    return [
        HistoryItem(
            action_id=a.id,
            comment_id=a.comment_id,
            action=a.action_type,
            actor=a.actor,
            note=a.note,
            youtube_synced=bool(a.youtube_synced),
            executed_at=a.executed_at,
            content=c.content,
            author_name=c.author_name,
            category=ra.category if ra else None,
            reason=ra.reasoning if ra else None,
        )
        for a, c, ra in rows
    ]


class SimilarCase(BaseModel):
    comment_id: int
    content: str
    similarity: float = Field(..., description="1.0 에 가까울수록 비슷하다")

    category: str | None
    status: str = Field(..., description="passed / queued / hidden")
    reviewed: bool = Field(..., description="관리자가 실제로 처리한 건인지")
    action: str | None = Field(None, description="hide / keep / ban_author")
    actor: str | None
    note: str | None
    executed_at: datetime | None


@router.get(
    "/comments/{comment_id}/similar",
    response_model=list[SimilarCase],
    summary="과거 유사 사례 (F_R_114)",
)
async def similar(
    target: Comment = Depends(require_comment),
    limit: int = Query(3, ge=1, le=10),
    # 최소 유사도로 자르지 않는다. 어디서 잘라야 하는지 근거가 아직 없다 —
    # 600건을 재보니 0.8 이상만 확실했고(카테고리 일치 93~97%), 그 아래는
    # 무작위로 두 건을 집었을 때의 일치율(59%)과 구분이 안 됐다.
    # 사람이 '참고가 됐다/안 됐다'를 라벨링해야 제대로 정할 수 있다.
    # 그때까지는 유사도 값을 그대로 내보내고 판단은 관리자에게 맡긴다.
    only_reviewed: bool = Query(
        False, description="관리자가 실제로 처리한 것만 보기"
    ),
    db: AsyncSession = Depends(get_db),
):
    """뜻이 비슷한 과거 댓글과, 그때 관리자가 내린 결정을 함께 돌려준다.

    단어가 겹치는 댓글이 아니라 '같은 사안'을 찾는 게 목적이다 —
    "주소 알아내서 찾아간다"와 "어디 사는지 알아냈음"은 겹치는 단어가
    없지만 관리자에겐 같은 건이다.

    같은 채널 안에서만 찾는다. 채널(고객사)간 데이터 결합·비교 노출은
    YouTube API 정책상 금지다.
    """
    comment_id = target.id
    if target.embedding is None:
        # 아직 벡터가 없으면 빈 목록. 화면은 '유사 사례 없음'으로 그리면 된다.
        return []

    latest = _latest_assessment()
    dist = Comment.embedding.cosine_distance(target.embedding)

    where = [
        Comment.channel_id == target.channel_id,
        Comment.id != comment_id,
        Comment.embedding.isnot(None),
    ]
    if only_reviewed:
        where.append(Comment.reviewed_at.isnot(None))

    # 조치 이력은 댓글당 여러 개일 수 있다. 가장 최근 것만 붙인다.
    last_action = (
        select(Action.comment_id.label("cid"), func.max(Action.id).label("aid"))
        .group_by(Action.comment_id)
        .subquery()
    )

    rows = (
        await db.execute(
            select(Comment, RiskAssessment, Action, dist.label("dist"))
            .outerjoin(latest, latest.c.cid == Comment.id)
            .outerjoin(RiskAssessment, RiskAssessment.id == latest.c.rid)
            .outerjoin(last_action, last_action.c.cid == Comment.id)
            .outerjoin(Action, Action.id == last_action.c.aid)
            .where(*where)
            .order_by(dist)
            .limit(limit)
        )
    ).all()

    return [
        SimilarCase(
            comment_id=c.id,
            content=c.content,
            similarity=round(1 - float(d), 4),
            category=ra.category if ra else None,
            status=c.status,
            reviewed=c.reviewed_at is not None,
            action=a.action_type if a else None,
            actor=a.actor if a else None,
            note=a.note if a else None,
            executed_at=a.executed_at if a else None,
        )
        for c, ra, a, d in rows
    ]
