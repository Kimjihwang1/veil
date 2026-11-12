"""등록된 단어 → 정규식 변환.

관리자는 `시발` 하나만 입력한다. 그 한 단어로
`ㅅㅂ`, `시 발`, `시1발`, `시이이이발` 까지 잡히도록 정규식을 만들어주는 게 이 모듈의 일.
"""

import re
import unicodedata

# 한글 완성형 유니코드는 (초성×588 + 중성×28 + 종성) 규칙으로 배치돼 있어서
# 산수만으로 초성·중성을 뽑을 수 있다. 사전도 외부 라이브러리도 필요 없다.
HANGUL_START = 0xAC00
HANGUL_END = 0xD7A3
CHOSUNG = "ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ"
JUNGSUNG = "ㅏㅐㅑㅒㅓㅔㅕㅖㅗㅘㅙㅚㅛㅜㅝㅞㅟㅠㅡㅢㅣ"
IEUNG_INDEX = CHOSUNG.index("ㅇ")

# 겹자음 한 글자를 낱자 두 개로 편다.
# ㄵ, ㅄ 같은 글자는 눈으로는 ㄴ+ㅈ, ㅂ+ㅅ 이지만 유니코드상 별개의 단일 문자다.
# 펴주지 않으면 "ㄴㅈ"은 잡히는데 "ㄵ"은 빠져나간다 (실제로 쓰이는 우회법).
COMPOUND_JAMO = {
    "ㄳ": "ㄱㅅ", "ㄵ": "ㄴㅈ", "ㄶ": "ㄴㅎ", "ㄺ": "ㄹㄱ", "ㄻ": "ㄹㅁ",
    "ㄼ": "ㄹㅂ", "ㄽ": "ㄹㅅ", "ㄾ": "ㄹㅌ", "ㄿ": "ㄹㅍ", "ㅀ": "ㄹㅎ",
    "ㅄ": "ㅂㅅ",
}

# 글자 사이에 끼어들 수 있는 것: 공백, 숫자, 특수문자, 밑줄.
# 한글/영문(=\w)은 제외한다. 그래야 "시x발"까지 잡는 과잉 매칭을 막는다.
FILLER = r"[\s\d\W_]*"

# 영문 단어는 대소문자를 구분하지 않는다 (bitch / BITCH / Bitch).
# 한글은 대소문자가 없으므로 영향 없다.
FLAGS = re.IGNORECASE


def normalize(text: str) -> str:
    """매칭 전에 텍스트를 정리한다.

    NFC로 모으는 이유: 자모를 따로 입력한 "ㅅㅣㅂㅏㄹ" 형태를 완성형으로 되돌린다.
    그다음 겹자음을 낱자로 편다.
    """
    text = unicodedata.normalize("NFC", text)
    return "".join(COMPOUND_JAMO.get(c, c) for c in text)


def to_chosung(ch: str) -> str | None:
    """한글 한 글자의 초성을 반환. 한글이 아니면 None."""
    code = ord(ch)
    if HANGUL_START <= code <= HANGUL_END:
        return CHOSUNG[(code - HANGUL_START) // 588]
    return None


def to_vowel_syllable(ch: str) -> str | None:
    """그 글자의 모음만 남긴 글자를 반환. 시→이, 노→오, 발→아.

    "시이이이발"처럼 모음을 늘여 쓰는 우회를 잡는 데 쓴다.
    (한글이 아니면 None)
    """
    code = ord(ch)
    if not (HANGUL_START <= code <= HANGUL_END):
        return None
    jung = ((code - HANGUL_START) % 588) // 28
    return chr(HANGUL_START + IEUNG_INDEX * 588 + jung * 28)


def _piece(ch: str) -> str:
    """글자 하나를 '반복 허용' 정규식 조각으로 만든다.

    시  ->  시+이*     (시시시, 시이이이 둘 다 허용)
    b   ->  b+         (bbb 허용)
    """
    piece = re.escape(ch) + "+"
    vowel = to_vowel_syllable(ch)
    if vowel and vowel != ch:
        piece += re.escape(vowel) + "*"
    return piece


def expand(word: str, expand_variants: bool = True) -> str:
    """단어 하나를 정규식 문자열로 변환한다.

    expand_variants=False 면 입력한 그대로만 잡는다.
    고유명사나 URL처럼 변형이 없는 단어는 꺼두는 편이 오탐이 적다.
    """
    word = normalize(word.strip())
    if not word:
        raise ValueError("빈 단어는 등록할 수 없습니다")

    if not expand_variants:
        return re.escape(word)

    alternatives = [FILLER.join(_piece(c) for c in word)]

    # 초성 변형은 전부 한글이고 2글자 이상일 때만 만든다.
    # 1글자 초성(예: ㅅ)은 아무 데나 걸려서 쓸 수 없다.
    chosungs = [to_chosung(c) for c in word]
    if len(word) >= 2 and all(chosungs):
        alternatives.append(FILLER.join(_piece(c) for c in chosungs))  # type: ignore[arg-type]

    return "|".join(alternatives)


def compile_rule(word: str, expand_variants: bool = True) -> re.Pattern[str]:
    """등록 시점에 정규식이 유효한지 검증하는 용도."""
    return re.compile(expand(word, expand_variants), FLAGS)
