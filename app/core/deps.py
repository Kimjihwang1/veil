"""인증·권한 의존성.

여기가 데이터 격리의 유일한 관문이다. 채널을 다루는 엔드포인트는 반드시
`require_channel` 을 거쳐야 하고, 그러면 URL 로 남의 channel_id 를 넣어도
404 가 난다.

404 를 쓰는 이유: 403 을 주면 "그 채널은 존재한다"는 사실이 새어나간다.
남의 워크스페이스에 무엇이 있는지 알려줄 이유가 없다.
"""

from datetime import UTC, datetime

from fastapi import Cookie, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Channel, Comment, Session, User, WorkspaceMember
from app.db.session import get_db

COOKIE = "outlier_session"

# 워크스페이스 안에서의 권한 세기. 높을수록 많은 걸 할 수 있다.
ROLE_RANK = {"member": 0, "admin": 1, "owner": 2}


async def current_user(
    outlier_session: str | None = Cookie(default=None, alias=COOKIE),
    db: AsyncSession = Depends(get_db),
) -> User:
    """로그인한 사용자. 없으면 401."""
    if not outlier_session:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "로그인이 필요합니다")

    row = (
        await db.execute(
            select(Session, User)
            .join(User, User.id == Session.user_id)
            .where(Session.token == outlier_session)
        )
    ).first()
    if row is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "세션이 유효하지 않습니다")

    session, user = row
    now = datetime.now(UTC).replace(tzinfo=None)
    if session.expires_at <= now:
        # 만료된 세션은 그 자리에서 지운다. 쌓여봐야 쓸모가 없다.
        await db.delete(session)
        await db.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "세션이 만료되었습니다")

    # 마지막 사용 시각은 자주 쓰는 값이 아니라 1분에 한 번만 갱신한다
    # (요청마다 UPDATE 를 치면 읽기 전용 화면에서도 쓰기가 발생한다).
    if session.last_seen_at is None or (now - session.last_seen_at).total_seconds() > 60:
        session.last_seen_at = now
        await db.commit()

    return user


async def optional_user(
    outlier_session: str | None = Cookie(default=None, alias=COOKIE),
    db: AsyncSession = Depends(get_db),
) -> User | None:
    """로그인 여부만 알고 싶을 때. 없으면 None (401 을 던지지 않는다)."""
    if not outlier_session:
        return None
    try:
        return await current_user(outlier_session, db)
    except HTTPException:
        return None


async def my_workspace_ids(db: AsyncSession, user: User) -> list[int]:
    return list(
        (
            await db.execute(
                select(WorkspaceMember.workspace_id).where(
                    WorkspaceMember.user_id == user.id
                )
            )
        )
        .scalars()
        .all()
    )


async def require_channel(
    channel_id: int,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> Channel:
    """내 워크스페이스에 속한 채널만 돌려준다.

    남의 채널이면 '없다'고 답한다. 채널을 다루는 엔드포인트는 전부 이걸
    거쳐야 한다 — 하나라도 빠뜨리면 그 경로로 다른 고객사 데이터가 샌다.
    """
    channel = await db.get(Channel, channel_id)
    if channel is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"채널 {channel_id} 없음")

    if channel.workspace_id is None or channel.workspace_id not in await my_workspace_ids(
        db, user
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"채널 {channel_id} 없음")

    return channel


async def require_comment(
    comment_id: int,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> Comment:
    """내 채널에 달린 댓글만 돌려준다.

    댓글 단위 엔드포인트(조치·유사사례)는 URL 에 channel_id 가 없어서
    require_channel 을 못 쓴다. 댓글에서 채널을 거슬러 올라가 같은 확인을 한다.
    """
    comment = await db.get(Comment, comment_id)
    if comment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"댓글 {comment_id} 없음")

    channel = await db.get(Channel, comment.channel_id) if comment.channel_id else None
    if channel is None or channel.workspace_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"댓글 {comment_id} 없음")
    if channel.workspace_id not in await my_workspace_ids(db, user):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"댓글 {comment_id} 없음")

    return comment


def require_role(minimum: str):
    """조치처럼 되돌리기 어려운 일에 최소 권한을 건다.

    아직 어디에도 안 걸려 있다. 조치 엔드포인트(/comments/{id}/action)는
    URL 에 channel_id 가 없어서 이 의존성을 그대로 못 쓴다. 그래서 지금은
    워크스페이스에 속한 사람이면 role 이 member 여도 숨김·차단을 할 수 있다.
    member 를 읽기 전용으로 둘 거면 require_comment 기반으로 다시 짜야 한다 —
    그건 권한 정책이라 팀이 정한 뒤에 한다.
    """

    async def check(
        channel: Channel = Depends(require_channel),
        user: User = Depends(current_user),
        db: AsyncSession = Depends(get_db),
    ) -> Channel:
        role = (
            await db.execute(
                select(WorkspaceMember.role).where(
                    WorkspaceMember.workspace_id == channel.workspace_id,
                    WorkspaceMember.user_id == user.id,
                )
            )
        ).scalar_one_or_none()

        if ROLE_RANK.get(role or "", -1) < ROLE_RANK[minimum]:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, f"{minimum} 이상의 권한이 필요합니다"
            )
        return channel

    return check
