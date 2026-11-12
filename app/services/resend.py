"""유튜브에 못 간 숨김을 다시 보낸다.

권한(리프레시 토큰)이 만료된 동안 누른 숨김은 우리 기록만 '숨김' 이고 유튜브에는 그대로
보인다. 구글 앱이 '테스트' 상태라 토큰이 7일마다 죽어서 실제로 여러 번 겪었다
(9/22, 11/3). 전에는 [다시 연결] 뒤에 관리자가 숨김 목록에서 한 건씩 [유튜브에 다시
보내기] 를 눌러야 했다. 다시 연결했으면 그걸로 끝나야 한다 — 연결 콜백이 이걸 부른다.

대상: 지금 숨김 상태이고, 그 댓글의 마지막 조치가 '숨김' 인데 유튜브 반영에 실패한 것.
채널 차단(ban_author)은 다른 API 라 여기서 다루지 않는다.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import text as sq
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Action, Channel, Comment
from app.services import youtube_actions as yt
from app.services.auto_hide import BATCH
from app.services.collector import HELD

ACTOR = "자동(다시 연결)"


@dataclass
class ResendStats:
    tried: int = 0
    synced: int = 0
    failed: int = 0


async def resend_unsynced(db: AsyncSession, channel: Channel) -> ResendStats:
    st = ResendStats()
    if not channel.youtube_refresh_token:
        return st

    rows = (
        await db.execute(
            sq("""
                SELECT c.id, c.youtube_comment_id
                FROM comments c
                JOIN LATERAL (
                    SELECT action_type, youtube_synced FROM actions a
                    WHERE a.comment_id = c.id ORDER BY a.id DESC LIMIT 1
                ) last ON true
                WHERE c.channel_id = :cid AND c.status = 'hidden'
                  AND last.action_type = 'hide' AND last.youtube_synced IS NOT TRUE
                  AND c.youtube_comment_id IS NOT NULL
                ORDER BY c.id
            """),
            {"cid": channel.id},
        )
    ).all()
    if not rows:
        return st

    now = datetime.now(UTC).replace(tzinfo=None)
    for i in range(0, len(rows), BATCH):
        chunk = rows[i:i + BATCH]
        ok, note = False, None
        try:
            r = await yt.set_moderation(
                channel.youtube_refresh_token, [yid for _, yid in chunk], yt.HIDE
            )
            ok = r.ok
            if not ok:
                note = f"[유튜브 반영 실패: {r.detail}]"
        except yt.NotConnected as e:
            note = f"[미연동: {e}]"
        except Exception as e:  # 유튜브가 죽어도 연결 자체는 끝난 것이다
            note = f"[유튜브 오류: {type(e).__name__}]"
        for cid, _ in chunk:
            db.add(Action(
                comment_id=cid, action_type="hide", actor=ACTOR,
                note=note, youtube_synced=ok, executed_at=now,
            ))
        if ok:
            # 유튜브가 보류해 뒀던 것이면 이제 거부다 (auto_hide 와 같은 처리).
            await db.execute(
                update(Comment)
                .where(Comment.id.in_([cid for cid, _ in chunk]))
                .where(Comment.youtube_moderation.in_(HELD))
                .values(youtube_moderation=yt.HIDE)
            )
        st.tried += len(chunk)
        if ok:
            st.synced += len(chunk)
        else:
            st.failed += len(chunk)
    await db.commit()
    return st
