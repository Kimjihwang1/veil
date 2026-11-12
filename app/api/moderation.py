"""댓글 판정 (F_R_109)."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import current_user, my_workspace_ids
from app.db.models import Channel, User
from app.db.session import get_db
from app.schemas import CheckRequest, CheckResponse
from app.services.moderation import judge

router = APIRouter(prefix="/moderation", tags=["moderation"])


@router.post("/check", response_model=CheckResponse, summary="댓글 1건 판정")
async def check(
    payload: CheckRequest,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    # channel_id 가 URL 이 아니라 본문에 있어서 require_channel 을 못 쓴다.
    # 그래서 같은 검사를 여기서 직접 한다. 이게 빠져 있었다 — 로그인 없이
    # 아무 channel_id 를 넣으면 그 채널의 등록어가 matched_pattern 으로
    # 그대로 새어 나갔다. 다른 고객사 규칙을 밖에서 읽을 수 있는 구멍이었다.
    channel = await db.get(Channel, payload.channel_id)
    if (
        channel is None
        or channel.workspace_id is None
        or channel.workspace_id not in await my_workspace_ids(db, user)
    ):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"채널 {payload.channel_id} 없음"
        )

    v = await judge(db, payload.channel_id, payload.text)
    return CheckResponse(
        verdict=v.verdict,
        matched_rule_id=v.matched_rule_id,
        matched_pattern=v.matched_pattern,
        matched_text=v.matched_text,
        reason=v.reason,
    )
