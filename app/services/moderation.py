"""1차 규칙기반 판정 (F_R_109).

댓글 하나를 받아서, 그 채널에 등록된 단어와 대조한다.
LLM 판별은 여기서 부르지 않는다. 파이프라인이 이 결과를 보고 넘길지 정한다.
verdict 가 block 이면 확정이지만, review 는 확정이 아니라 'LLM이 문맥을
봐야 할 후보'라는 표시다 — 유튜버 이름 같은 민감어가 여기 해당한다.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ChannelRule
from app.services.pattern import FLAGS, normalize

# 검사 순서. 차단이 검토를 이긴다. (예외 동작은 2026-09-17 에 뺐다 — schemas.Action 참고)
ACTION_ORDER = ("block", "review")


@dataclass
class Verdict:
    verdict: str  # block / review / pass
    matched_rule_id: int | None = None
    matched_pattern: str | None = None
    matched_text: str | None = None
    reason: str = "등록된 단어 없음"


def judge_rules(rules: Sequence[ChannelRule], text: str) -> Verdict:
    """규칙 목록과 댓글을 대조한다. DB에 의존하지 않아 단위 테스트가 쉽다."""
    # 겹자음(ㄵ, ㅄ)을 펴고 자모를 모은 뒤에 매칭한다.
    text = normalize(text)
    enabled = [r for r in rules if r.enabled]

    for action in ACTION_ORDER:
        for rule in (r for r in enabled if r.action == action):
            hit = re.search(rule.compiled_regex, text, FLAGS)
            if not hit:
                continue

            return Verdict(
                verdict=action,
                matched_rule_id=rule.id,
                matched_pattern=rule.rule_value,
                matched_text=hit.group(),
                reason=f"관리자 등록 단어 '{rule.rule_value}'",
            )

    return Verdict(verdict="pass")


async def judge(db: AsyncSession, channel_id: int, text: str) -> Verdict:
    """DB에서 해당 채널 규칙만 불러와 판정한다.

    channel_id 필터를 빠뜨리면 다른 고객사 규칙이 섞인다 (정책상 금지).
    """
    rules = (
        (
            await db.execute(
                select(ChannelRule).where(ChannelRule.channel_id == channel_id)
            )
        )
        .scalars()
        .all()
    )
    return judge_rules(rules, text)
