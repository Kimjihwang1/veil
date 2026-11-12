"""판정 로직 테스트 (DB 불필요 — 규칙 객체를 직접 만든다)."""

import pytest

from app.db.models import ChannelRule
from app.services.moderation import judge_rules
from app.services.pattern import expand


def rule(rule_id, value, action, variants=True, enabled=True):
    return ChannelRule(
        id=rule_id,
        channel_id=1,
        rule_type="keyword",
        rule_value=value,
        compiled_regex=expand(value, variants),
        action=action,
        expand_variants=variants,
        enabled=enabled,
    )


GAME_CHANNEL = [
    rule(1, "시발", "block"),
    rule(3, "발컨", "review", variants=False),
]


@pytest.mark.parametrize(
    "text, expected",
    [
        ("ㅅㅂ 못하네", "block"),
        ("시1발 진짜", "block"),
        # 예외 동작을 뺐으므로 '시발점' 도 걸린다. 차단어는 그만큼 좁게 써야 한다는
        # 뜻이고, 그래서 차단은 기본 선택이 아니다 (검토가 기본).
        ("여기가 시발점이다", "block"),
        ("발컨이네 ㅋㅋ", "review"),
        ("오늘 영상 재밌네요", "pass"),
    ],
)
def test_game_channel(text, expected):
    assert judge_rules(GAME_CHANNEL, text).verdict == expected


def test_same_word_differs_by_channel():
    """같은 댓글이 채널 설정에 따라 다르게 판정되어야 한다."""
    idol_channel = [rule(10, "발컨", "block", variants=False)]

    assert judge_rules(GAME_CHANNEL, "발컨이네").verdict == "review"   # 게임 채널: 사람이 본다
    assert judge_rules(idol_channel, "발컨이네").verdict == "block"    # 아이돌 채널: 바로 가림


def test_review_action():
    rules = [rule(20, "전장연", "review", variants=False)]
    v = judge_rules(rules, "전장연 관련 영상")
    assert v.verdict == "review"
    assert v.matched_pattern == "전장연"
    assert v.matched_rule_id == 20


def test_disabled_rule_is_ignored():
    rules = [rule(30, "시발", "block", enabled=False)]
    assert judge_rules(rules, "시발").verdict == "pass"


def test_verdict_carries_reason():
    v = judge_rules(GAME_CHANNEL, "ㅅㅂ")
    assert v.matched_rule_id == 1
    assert v.matched_pattern == "시발"
    assert v.matched_text == "ㅅㅂ"


@pytest.mark.parametrize(
    "word, text",
    [
        ("병신", "ㅂㅅ이네"),   # 낱자 두 개
        ("병신", "ㅄ이네"),     # ㅂ+ㅅ 겹자음 한 글자 (U+3144)
        ("노잼", "ㄴㅈ이네"),
        ("노잼", "ㄵ이네"),     # ㄴ+ㅈ 겹자음 한 글자 (U+3135)
    ],
)
def test_compound_jamo_evasion(word, text):
    """ㄵ, ㅄ 같은 겹자음 한 글자로 우회하는 걸 막는다."""
    assert judge_rules([rule(40, word, "block")], text).verdict == "block"


def test_block_beats_review():
    """같은 댓글에 차단어와 검토어가 둘 다 걸리면 차단이 이긴다."""
    rules = [rule(50, "시발", "block"), rule(51, "뭐야", "review", variants=False)]
    assert judge_rules(rules, "시이이이발 뭐야").verdict == "block"


def test_allow_action_is_rejected_by_schema():
    """예외(allow) 는 2026-09-17 에 뺐다. API 가 받지 않아야 한다."""
    from pydantic import TypeAdapter, ValidationError

    from app.schemas import Action

    with pytest.raises(ValidationError):
        TypeAdapter(Action).validate_python("allow")
