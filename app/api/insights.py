"""인사이트 화면 — 기간별로 이 채널에 무슨 일이 있었나.

대시보드의 /stats 는 '지금 상태'(검토 대기 몇 건)를 센다. 여기는 '그 기간에
판별한 것'을 센다. 기준 시각은 판별 시각(risk_assessments.created_at)이다 —
오래된 영상의 댓글을 오늘 처음 판별했으면 오늘 치다.

재판별하면 판정 행이 쌓이므로 댓글마다 가장 최근 판정만 본다 (review._latest_assessment).
"""

from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.review import _latest_assessment
from app.core.deps import require_channel
from app.db.models import Channel, Comment, RiskAssessment, Video
from app.db.session import get_db
from app.services.severity import NAMES, level_of

router = APIRouter(tags=["insights"])

KST = timedelta(hours=9)
PERIOD = {"today": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30)}
HARMFUL = ("queued", "hidden")


class TrendPoint(BaseModel):
    label: str = Field(..., description="막대 아래 글자. 한국 시간 기준")
    n: int = Field(..., description="그 구간에 판별해 검토 큐·숨김으로 간 댓글 수")


class VideoRow(BaseModel):
    video_id: str | None
    title: str
    analyzed: int
    queued: int = Field(..., description="아직 관리자가 안 본 검토 대기")


class Insights(BaseModel):
    channel_id: int
    period: str
    analyzed: int = Field(..., description="기간 안에 판별한 댓글")
    queued: int = Field(..., description="그중 지금 검토 큐에 있는 것")
    unreviewed: int = Field(..., description="기간과 무관하게 지금 관리자를 기다리는 것")
    hidden: int = Field(..., description="그중 지금 숨김 상태인 것")
    hidden_manual: int = Field(..., description="숨김 중 사람이 누른 것. 나머지는 등록어·자동 숨김")
    levels: dict[str, int] = Field(..., description="위험도 1~5 별 건수 (키는 '1'~'5'). 통과한 것은 1")
    categories: list[tuple[str, int]] = Field(..., description="검토 큐·숨김으로 간 것의 분류, 많은 순")
    trend: list[TrendPoint]
    videos: list[VideoRow] = Field(..., description="검토 대기 많은 순")


def _level(category: str | None, label: str | None, severity: int | None, status: str) -> int:
    """검토 큐(review._level)와 같은 규칙. 통과한 것은 위험도가 없으니 1."""
    if status == "passed":
        return 1
    return level_of(severity, category, label)


def _buckets(period: str, now_kst: datetime):
    """(구간 수, 시각→구간 번호, 구간 번호→글자)."""
    if period == "today":
        start = now_kst - timedelta(hours=24)
        idx = lambda t: int((t - start).total_seconds() // (4 * 3600))  # noqa: E731
        label = lambda i: f"{(start + timedelta(hours=4 * i)).hour}시"  # noqa: E731
        return 6, idx, label
    days, width = (7, 1) if period == "7d" else (30, 6)
    first = (now_kst - timedelta(days=days - 1)).date()
    idx = lambda t: (t.date() - first).days // width  # noqa: E731

    def label(i):
        d = first + timedelta(days=i * width)
        return f"{d.month}/{d.day}" + ("~" if width > 1 else "")

    return days // width, idx, label


@router.get(
    "/channels/{channel_id}/insights", response_model=Insights, summary="기간별 인사이트"
)
async def insights(
    channel: Channel = Depends(require_channel),
    period: Literal["today", "7d", "30d"] = Query("7d"),
    db: AsyncSession = Depends(get_db),
):
    now = datetime.now(UTC).replace(tzinfo=None)
    since = now - PERIOD[period]
    latest = _latest_assessment()

    rows = (
        await db.execute(
            select(
                Comment.status, Comment.reviewed_at, Comment.video_id,
                RiskAssessment.category, RiskAssessment.risk_level,
                RiskAssessment.destination, RiskAssessment.created_at,
                RiskAssessment.severity,
            )
            .join(latest, latest.c.cid == Comment.id)
            .join(RiskAssessment, RiskAssessment.id == latest.c.rid)
            .where(Comment.channel_id == channel.id, RiskAssessment.created_at >= since)
        )
    ).all()

    unreviewed = (
        await db.execute(
            select(func.count()).select_from(Comment).where(
                Comment.channel_id == channel.id,
                Comment.status == "queued",
                Comment.reviewed_at.is_(None),
            )
        )
    ).scalar_one()

    n_buckets, idx, label = _buckets(period, now + KST)
    trend = [0] * n_buckets
    levels = Counter({str(k): 0 for k in NAMES})
    cats: Counter = Counter()
    per_video: dict[str | None, list[int]] = {}
    queued = hidden = hidden_manual = 0

    for st, reviewed_at, vid, cat, label_, dest, at, sev in rows:
        levels[str(_level(cat, label_, sev, st))] += 1
        v = per_video.setdefault(vid, [0, 0])
        v[0] += 1
        if st == "queued":
            queued += 1
            if reviewed_at is None:
                v[1] += 1
        if st == "hidden":
            hidden += 1
            # 판별이 바로 숨김으로 보낸 것(등록어 차단·자동 숨김)이 아니면 사람이 누른 것이다.
            if dest != "hidden":
                hidden_manual += 1
        if st in HARMFUL:
            if cat and cat != "정상":
                cats[cat] += 1
            i = idx(at + KST)
            if 0 <= i < n_buckets:
                trend[i] += 1

    titles = dict(
        (
            await db.execute(
                select(Video.video_id, Video.title).where(
                    Video.video_id.in_([v for v in per_video if v])
                )
            )
        ).all()
    ) if per_video else {}

    videos = sorted(
        (
            VideoRow(video_id=vid, title=titles.get(vid) or vid or "영상 정보 없음",
                     analyzed=a, queued=q)
            for vid, (a, q) in per_video.items()
        ),
        key=lambda r: (-r.queued, -r.analyzed),
    )

    return Insights(
        channel_id=channel.id,
        period=period,
        analyzed=len(rows),
        queued=queued,
        unreviewed=unreviewed,
        hidden=hidden,
        hidden_manual=hidden_manual,
        levels=dict(levels),
        categories=cats.most_common(),
        trend=[TrendPoint(label=label(i), n=n) for i, n in enumerate(trend)],
        videos=videos,
    )
