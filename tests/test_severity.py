"""위험도 1~5 (2026-11-09).

DB 도 LLM 도 안 쓴다. 지키려는 것 셋 — 단계를 어떻게 정하는가, 모델이 준 값을
어디까지 믿는가, 프롬프트 예시가 정의와 어긋나지 않는가.
"""

import re

from app.services import llm
from app.services.severity import FALLBACK, level_of, needs_evidence, raw_level


class TestLevel:
    def test_AI가_매긴_값을_먼저_쓴다(self):
        assert level_of(2, "위협", "harmful") == 2   # 표로는 5지만 댓글을 읽은 값이 이긴다

    def test_값이_없으면_카테고리_표(self):
        assert raw_level(None, "모욕") == FALLBACK["모욕"] == 3
        assert raw_level(None, None) == 3             # 등록어처럼 카테고리도 없는 것

    def test_애매는_한_단계_내리되_1_밑으로는_안_간다(self):
        assert level_of(5, "자해", "ambiguous") == 4
        assert level_of(1, "스팸", "ambiguous") == 1

    def test_범위_밖_값은_없는_것으로(self):
        assert raw_level(0, "혐오") == 4
        assert raw_level(7, "혐오") == 4


class TestEvidenceRule:
    def test_유해_4_이상만_자동_증거(self):
        assert needs_evidence(4, "harmful")
        assert not needs_evidence(3, "harmful")

    def test_애매는_자동으로_남기지_않는다(self):
        assert not needs_evidence(5, "ambiguous")

    def test_표로_추정한_단계로는_남기지_않는다(self):
        # 값이 없으면(옛 판정·위험도를 안 주는 판별기) 카테고리가 위협이어도 자동 저장하지 않는다.
        assert not needs_evidence(None, "harmful")


class TestParse:
    def test_정상은_항상_None(self):
        assert llm._severity(4, "safe") is None

    def test_유해인데_0이면_믿지_않는다(self):
        assert llm._severity(0, "harmful") is None

    def test_숫자로_읽히면_받는다(self):
        assert llm._severity("3", "harmful") == 3
        assert llm._severity(9, "harmful") is None
        assert llm._severity("x", "ambiguous") is None


class TestPrompt:
    def test_스키마가_위험도를_반드시_받는다(self):
        s = llm.SCHEMA["schema"]
        assert "severity" in s["required"]
        assert s["properties"]["severity"]["enum"] == [0, 1, 2, 3, 4, 5]

    def test_정의가_프롬프트에_있다(self):
        p = llm.build_prompt()
        assert "[위험도]" in p
        for n in range(1, 6):
            assert re.search(rf"^{n} \S+ :", p, re.M), f"{n}단계 정의가 없다"

    def test_예시마다_단계가_있고_정상은_0(self):
        # "예시가 원칙을 이긴다" — 예시 하나라도 형식이 어긋나면 모델이 그쪽을 따른다.
        lines = re.findall(r"^-> (\w+) / (\S+) / (\d)\.", llm.EXAMPLES, re.M)
        arrows = re.findall(r"^-> ", llm.EXAMPLES, re.M)
        assert len(lines) == len(arrows) == 23
        for label, _, lv in lines:
            assert (lv == "0") == (label == "safe")
