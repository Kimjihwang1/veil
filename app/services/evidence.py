"""고소용 증거 저장 (2026-11-09).

두 길로 들어온다.
  자동   판별하는 순간. 위험도 4 이상으로 확정된 유해 댓글 (store.save_results)
  수동   관리자가 어느 댓글에서든 [증거로 저장] 을 누름 (api/evidence.py)

왜 comments 와 따로 두는지, 왜 30일인지는 models.Evidence 주석 참고.

고소장에 붙일 때 필요한 것: URL, 작성자 아이디, 작성 일시, 전체 내용. 부분 캡처는
증거 능력이 떨어지고, URL 이 없으면 영장 청구가 거부될 수 있다고 한다 (법률사무소
안내 글 기준 — 실제 절차는 변호사 확인이 필요하다). 그래서 이 넷은 빠짐없이 남긴다.
"""

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from app.db.models import Comment, Evidence, RiskAssessment, Video
from app.services.collector import RETENTION_DAYS
from app.services.retention import MARK


def content_hash(
    youtube_comment_id: str,
    author_channel_id: str | None,
    published_at: datetime | None,
    content: str,
) -> str:
    """누가 · 언제 · 무엇을 썼는지의 SHA-256.

    내려받은 파일의 원문을 고쳤는지 나중에 확인할 수 있게 같이 적어 둔다.
    같은 댓글이 수정되면 값이 달라져 새 증거 행이 생긴다 — 고치기 전 글이 남는다.
    """
    raw = "\n".join([
        youtube_comment_id,
        author_channel_id or "",
        published_at.isoformat() if published_at else "",
        content,
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def comment_url(video_id: str | None, youtube_comment_id: str) -> str | None:
    """댓글 하나로 바로 가는 주소. lc= 가 그 댓글을 맨 위에 띄운다."""
    if not video_id:
        return None
    return f"https://www.youtube.com/watch?v={video_id}&lc={youtube_comment_id}"


def author_url(author_channel_id: str | None) -> str | None:
    """닉네임은 바뀌어도 채널 ID 는 안 바뀐다. 피고소인 특정에 이쪽이 낫다."""
    return f"https://www.youtube.com/channel/{author_channel_id}" if author_channel_id else None


async def capture(
    db,
    comment_ids: Sequence[int],
    source: str,
    saved_by: str | None = None,
    note: str | None = None,
) -> list[int]:
    """댓글들을 지금 모습 그대로 증거로 남긴다. 새로 남긴 evidence id 를 돌려준다.

    같은 원문이 이미 저장돼 있으면 건너뛴다 (comment_id + 해시가 같으면).
    보관기한이 지나 원문을 지운 댓글은 남길 게 없어서 건너뛴다.
    커밋은 부르는 쪽이 한다.
    """
    if not comment_ids:
        return []

    latest = (
        select(RiskAssessment.comment_id.label("cid"), func.max(RiskAssessment.id).label("rid"))
        .where(RiskAssessment.comment_id.in_(comment_ids))
        .group_by(RiskAssessment.comment_id)
        .subquery()
    )
    parent = Comment.__table__.alias("parent")
    rows = (
        await db.execute(
            select(Comment, RiskAssessment, Video.title, parent.c.content)
            .outerjoin(latest, latest.c.cid == Comment.id)
            .outerjoin(RiskAssessment, RiskAssessment.id == latest.c.rid)
            .outerjoin(Video, Video.video_id == Comment.video_id)
            .outerjoin(parent, parent.c.youtube_comment_id == Comment.parent_comment_id)
            .where(Comment.id.in_(comment_ids))
        )
    ).all()

    now = datetime.now(UTC).replace(tzinfo=None)
    values = []
    for c, ra, title, parent_content in rows:
        if not c.content or c.content == MARK:
            continue
        values.append(dict(
            channel_id=c.channel_id,
            comment_id=c.id,
            youtube_comment_id=c.youtube_comment_id,
            video_id=c.video_id,
            video_title=title,
            author_name=c.author_name,
            author_channel_id=c.author_channel_id,
            content=c.content,
            parent_content=parent_content if parent_content != MARK else None,
            published_at=c.published_at,
            content_updated_at=c.content_updated_at,
            label=ra.risk_level if ra else None,
            category=ra.category if ra else None,
            severity=ra.severity if ra else None,
            reasoning=ra.reasoning if ra else None,
            source=source,
            saved_by=saved_by,
            note=note,
            content_hash=content_hash(
                c.youtube_comment_id, c.author_channel_id, c.published_at, c.content
            ),
            captured_at=now,
            # 원래 댓글과 같은 날 지운다. 기한이 없는 옛 행이면 지금부터 30일.
            expires_at=c.retention_expires_at or now + timedelta(days=RETENTION_DAYS),
        ))
    if not values:
        return []

    stmt = (
        insert(Evidence)
        .values(values)
        .on_conflict_do_nothing(index_elements=["comment_id", "content_hash"])
        .returning(Evidence.id)
    )
    return list((await db.execute(stmt)).scalars().all())
