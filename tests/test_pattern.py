"""정규식 생성 테스트 (DB 불필요)."""

import re

import pytest

from app.services.pattern import FLAGS, expand, normalize, to_chosung, to_vowel_syllable


@pytest.mark.parametrize(
    "text, should_match",
    [
        ("시발 못하네", True),      # 원형
        ("시 발 못하네", True),     # 공백
        ("시1발 못하네", True),     # 숫자
        ("시*발 못하네", True),     # 특수문자
        ("ㅅㅂ 못하네", True),      # 초성
        ("시x발 못하네", False),    # 글자가 끼면 다른 말로 본다
        ("아무 문제 없는 댓글", False),
    ],
)
def test_expand_variants(text, should_match):
    pattern = re.compile(expand("시발", expand_variants=True))
    assert bool(pattern.search(text)) is should_match


@pytest.mark.parametrize(
    "text, should_match",
    [
        ("시발 못하네", True),
        ("시 발 못하네", False),   # 확장을 끄면 원형만
        ("ㅅㅂ 못하네", False),
    ],
)
def test_expand_disabled(text, should_match):
    pattern = re.compile(expand("시발", expand_variants=False))
    assert bool(pattern.search(text)) is should_match


def test_special_chars_are_escaped():
    """URL처럼 정규식 특수문자가 든 단어도 안전해야 한다."""
    pattern = re.compile(expand("bit.ly", expand_variants=False))
    assert pattern.search("여기 bit.ly/abc")
    assert not pattern.search("bitxly")  # '.'이 와일드카드로 동작하면 안 됨


def test_chosung():
    assert to_chosung("시") == "ㅅ"
    assert to_chosung("발") == "ㅂ"
    assert to_chosung("a") is None


def test_single_char_has_no_chosung_variant():
    """1글자는 초성 변형을 만들지 않는다 (아무 데나 걸려서 못 씀)."""
    assert "ㅅ" not in expand("시", expand_variants=True)


def test_empty_word_rejected():
    with pytest.raises(ValueError):
        expand("   ")


@pytest.mark.parametrize(
    "text",
    [
        "ㄴㅈ이네",   # 낱자 두 개
        "ㄵ이네",     # 겹자음 한 글자 (U+3135)
    ],
)
def test_compound_jamo_is_expanded(text):
    """ㄵ는 ㄴ+ㅈ으로 보이지만 유니코드상 한 글자라 따로 펴줘야 잡힌다."""
    pattern = re.compile(expand("노잼", expand_variants=True))
    assert pattern.search(normalize(text))


def test_compound_jamo_in_registered_word():
    """관리자가 'ㅄ'을 등록해도 'ㅂㅅ'으로 쓴 댓글이 잡혀야 한다."""
    pattern = re.compile(expand("ㅄ", expand_variants=True))
    assert pattern.search(normalize("ㅂㅅ아"))
    assert pattern.search(normalize("ㅄ아"))


@pytest.mark.parametrize(
    "text",
    ["시발", "시이이이발", "시시시발", "ㅅㅂ", "ㅅㅅㅂ", "시  발", "시!@#발"],
)
def test_repeat_and_stretch(text):
    """같은 글자 반복(시시발)과 모음 늘이기(시이이발) 둘 다 잡는다."""
    assert re.search(expand("시발"), normalize(text), FLAGS)


@pytest.mark.parametrize("text", ["시원한 발", "시장에 갔다", "오늘 영상 재밌네요"])
def test_repeat_does_not_overmatch(text):
    assert not re.search(expand("시발"), normalize(text), FLAGS)


def test_vowel_syllable():
    assert to_vowel_syllable("시") == "이"
    assert to_vowel_syllable("노") == "오"
    assert to_vowel_syllable("발") == "아"
    assert to_vowel_syllable("a") is None


@pytest.mark.parametrize("text", ["BITCH", "Bitch", "bitch", "biiitch"])
def test_case_insensitive(text):
    """영문은 대소문자를 구분하지 않는다."""
    assert re.search(expand("bitch"), normalize(text), FLAGS)


def test_case_insensitive_no_overmatch():
    assert not re.search(expand("bitch"), normalize("switch 샀다"), FLAGS)
