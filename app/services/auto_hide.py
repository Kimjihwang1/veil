"""자동 숨김(차단어 · 자동 숨김 분류)을 유튜브에 실제로 반영한다.

판별 파이프라인이 행선지를 '숨김' 으로 정해도 그건 우리 DB 의 상태일 뿐이다.
유튜브에서 실제로 가리는 호출은 관리자 버튼 경로(api/review.py)에만 있었고
자동 경로(watcher · run_pipeline)에는 없었다 — 2026-09-30 발견. 관리 기준 화면에
토글이 있고 숨김 목록에도 들어가는데 유튜브에는 그대로 보이는 거짓 상태였다.

여기서는 관리자 버튼과 같은 함수(youtube_actions.set_moderation)로 가리고,
결과를 actions 에 남긴다. 그래야 처리 이력에 "자동(등록어: 시발)" 처럼 보이고
반영 여부(youtube_synced)도 같은 칸으로 읽힌다.

- 토큰이 없는 채널(API 키로 수집만 한 샘플 채널)은 호출하지 않고 '미연동' 으로 기록.
- 관리자가 이미 처리한 댓글(reviewed_at 있음)은 건드리지 않는다. 사람 판단 우선.
- 유튜브가 실패해도 우리 DB 의 숨김 상태는 그대로 둔다. 관리자가 숨김 목록에서
  '미반영' 을 보고 다시 누를 수 있다.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Action, Channel, Comment
from app.services import youtube_actions as yt
from app.services.collector import HELD
from app.services.pipeline import PipelineResult

# setModerationStatus 는 id 를 쉼표로 여러 개 받는다. 한 번에 너무 많이 보내면
# URL 이 길어지므로 관리자 화면과 같은 크기로 끊는다.
BATCH = 50


@dataclass
class AutoHideStats:
    attempted: int = 0      # 숨김 행선지였던 댓글 수
    synced: int = 0         # 유튜브에 실제로 가려진 수
    failed: int = 0         # 호출했는데 실패
    not_connected: int = 0  # 토큰 없어 호출 안 함
    skipped: int = 0        # 관리자가 이미 처리해서 건드리지 않음
    notes: list[str] = field(default_factory=list)


def _actor(r: PipelineResult) -> str:
    if r.decided_by == "rule":
        return f"자동(등록어: {r.rule_value})"
    return f"자동(분류: {r.llm_category or r.llm_label})"


async def apply_auto_hides(
    db: AsyncSession,
    channel: Channel,
    pairs: list[tuple[int, PipelineResult]],
) -> AutoHideStats:
    """(comment_id, 판별결과) 중 숨김 행선지인 것을 유튜브에 반영하고 이력을 남긴다.

    save_results() 뒤에 부른다. 같은 세션에서 commit 까지 한다.
    """
    st = AutoHideStats()
    hidden = [(cid, r) for cid, r in pairs if r.is_hidden]
    if not hidden:
        return st

    rows = (
        await db.execute(
            select(Comment).where(Comment.id.in_([cid for cid, _ in hidden]))
        )
    ).scalars().all()
    by_id = {c.id: c for c in rows}
    now = datetime.now(UTC).replace(tzinfo=None)

    targets: list[tuple[Comment, PipelineResult]] = []
    for cid, r in hidden:
        c = by_id.get(cid)
        if c is None:
            continue
        if c.reviewed_at is not None:
            st.skipped += 1
            continue
        targets.append((c, r))
    st.attempted = len(targets)
    if not targets:
        return st

    # 채널에 권한이 없으면 호출 없이 기록만. 화면은 이걸 '미반영' 으로 그린다.
    if not channel.youtube_refresh_token:
        st.not_connected = len(targets)
        for c, r in targets:
            db.add(Action(
                comment_id=c.id, action_type="hide", actor=_actor(r),
                note=f"{r.reason} [미연동: 채널 소유자 연동이 안 돼 유튜브에 반영 안 됨]",
                youtube_synced=False, executed_at=now,
            ))
        await db.commit()
        return st

    for i in range(0, len(targets), BATCH):
        chunk = targets[i:i + BATCH]
        ids = [c.youtube_comment_id for c, _ in chunk if c.youtube_comment_id]
        ok, detail = False, ""
        try:
            res = await yt.set_moderation(channel.youtube_refresh_token, ids, yt.HIDE)
            ok, detail = res.ok, res.detail
        except yt.NotConnected as e:
            detail = f"미연동: {e}"
        except Exception as e:  # 유튜브가 죽어도 판정과 이력은 남긴다
            detail = f"유튜브 오류: {type(e).__name__}"

        for c, r in chunk:
            note = r.reason
            if not ok:
                note = f"{note} [유튜브 반영 실패: {detail}]".strip()
            elif c.youtube_moderation in HELD:
                # 유튜브가 보류해 둔 걸 가렸다. 더는 보류가 아니라 거부다.
                c.youtube_moderation = yt.HIDE
            db.add(Action(
                comment_id=c.id, action_type="hide", actor=_actor(r),
                note=note, youtube_synced=ok, executed_at=now,
            ))
        if ok:
            st.synced += len(chunk)
        else:
            st.failed += len(chunk)
            st.notes.append(detail)

    await db.commit()
    return st
