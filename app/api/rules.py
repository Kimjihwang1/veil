"""채널별 단어 규칙 CRUD (F_R_108)."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Channel, ChannelRule
from app.core.deps import require_channel
from app.db.session import get_db
from app.schemas import RuleCreate, RuleOut, RuleUpdate
from app.services.pattern import expand

router = APIRouter(prefix="/channels/{channel_id}/rules", tags=["rules"])


@router.get("", response_model=list[RuleOut], summary="등록된 단어 목록")
async def list_rules(
    channel: Channel = Depends(require_channel), db: AsyncSession = Depends(get_db)
):
    channel_id = channel.id
    result = await db.execute(
        select(ChannelRule)
        .where(ChannelRule.channel_id == channel_id)
        .order_by(ChannelRule.id)
    )
    return result.scalars().all()


@router.post(
    "", response_model=RuleOut, status_code=status.HTTP_201_CREATED, summary="단어 등록"
)
async def create_rule(
    payload: RuleCreate,
    channel: Channel = Depends(require_channel),
    db: AsyncSession = Depends(get_db),
):
    channel_id = channel.id

    rule = ChannelRule(
        channel_id=channel_id,
        rule_type="keyword",
        rule_value=payload.rule_value.strip(),
        # 정규식은 등록할 때 한 번만 만들어 저장한다.
        compiled_regex=expand(payload.rule_value, payload.expand_variants),
        action=payload.action,
        expand_variants=payload.expand_variants,
        enabled=True,
    )
    db.add(rule)
    await db.commit()
    await db.refresh(rule)
    return rule


@router.patch("/{rule_id}", response_model=RuleOut, summary="단어 수정")
async def update_rule(
    rule_id: int,
    payload: RuleUpdate,
    channel: Channel = Depends(require_channel),
    db: AsyncSession = Depends(get_db),
):
    rule = await db.get(ChannelRule, rule_id)
    if rule is None or rule.channel_id != channel.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"규칙 {rule_id} 없음")

    if payload.action is not None:
        rule.action = payload.action
    if payload.enabled is not None:
        rule.enabled = payload.enabled
    if payload.expand_variants is not None:
        rule.expand_variants = payload.expand_variants
        # 확장 여부가 바뀌면 정규식을 다시 만들어야 한다.
        rule.compiled_regex = expand(rule.rule_value, payload.expand_variants)

    await db.commit()
    await db.refresh(rule)
    return rule


@router.delete(
    "/{rule_id}", status_code=status.HTTP_204_NO_CONTENT, summary="단어 삭제"
)
async def delete_rule(
    rule_id: int,
    channel: Channel = Depends(require_channel),
    db: AsyncSession = Depends(get_db),
):
    rule = await db.get(ChannelRule, rule_id)
    if rule is None or rule.channel_id != channel.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"규칙 {rule_id} 없음")
    await db.delete(rule)
    await db.commit()
