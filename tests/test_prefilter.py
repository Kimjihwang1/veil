"""GPT 앞단 전처리·규칙 테스트 (DB·모델 파일 불필요)."""

import pytest

from app.services.prefilter import normalize, nothing_to_judge


@pytest.mark.parametrize(
    "text, expected",
    [
        ("ㅋㅋㅋㅋㅋㅋㅋㅋ 웃기네", "ㅋㅋㅋ 웃기네"),         # 긴 반복은 3번으로
        ("ㅋㅋㅋ 웃기네", "ㅋㅋㅋ 웃기네"),                # 3번은 그대로
        ("@홍길동 그만해", "@사용자 그만해"),              # GPT 에 보내는 것과 같게
        ("여기 https://a.com/x 봐", "여기 URL 봐"),
        ("  줄\n\n바꿈   많은  댓글 ", "줄 바꿈 많은 댓글"),
        ("가", "가"),                           # 풀어쓴 자모 → 완성형 (NFC)
    ],
)
def test_normalize(text, expected):
    assert normalize(text) == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("😂😂", True),
        ("ㅋㅋㅋㅋ", True),
        ("12:34 ??", True),
        (".", True),
        ("ㅅㅂ", False),       # 의미 있는 자모
        ("ㄹㅇㅋㅋ", False),   # ㄹ, ㅇ 은 감정 자모가 아니다
        ("凸", False),         # 기호 욕
        ("山", False),
        ("Tlqkf", False),      # 영문 자판 우회
        ("좋아요", False),
    ],
)
def test_nothing_to_judge(text, expected):
    assert nothing_to_judge(text) is expected


# ── 파이프라인 연결 ─────────────────────────────────────────────

from types import SimpleNamespace

from app.services.pipeline import PASSED, QUEUE_INFO, process
from app.services.prefilter import Prefilter


class FakeModel:
    """sklearn 모델 대신. 정해 둔 유해 확률을 돌려준다."""

    def __init__(self, p: float):
        self.p = p

    def predict_proba(self, xs):
        import numpy as np
        return np.array([[1 - self.p, self.p] for _ in xs])


class CountingJudge:
    """LLM 대신. 몇 번 불렸는지 센다."""

    def __init__(self, label="safe"):
        self.calls = 0
        self.label = label

    async def judge(self, text, parent_text=None, video=None):
        self.calls += 1
        return SimpleNamespace(label=self.label, category="정상", reason="", error=None)


def review_rule(word: str):
    from app.services.pattern import expand
    return SimpleNamespace(id=1, rule_value=word, action="review", enabled=True,
                           compiled_regex=expand(word))


@pytest.mark.asyncio
class TestPipelineWithPrefilter:
    async def test_on_이면_확실히_정상은_GPT_안_부름(self):
        j = CountingJudge()
        r = await process([], j, "영상 재밌네요", prefilter=Prefilter("on", 0.1, FakeModel(0.02)))
        assert j.calls == 0
        assert r.destination == PASSED and r.decided_by == "prefilter"
        assert r.prefilter_score == pytest.approx(0.02)

    async def test_on_이어도_문턱값_넘으면_GPT로(self):
        j = CountingJudge()
        r = await process([], j, "저 사람 왜 저래", prefilter=Prefilter("on", 0.1, FakeModel(0.4)))
        assert j.calls == 1 and r.decided_by == "llm"

    async def test_shadow_는_점수만_남기고_GPT_그대로(self):
        j = CountingJudge()
        r = await process([], j, "영상 재밌네요", prefilter=Prefilter("shadow", 0.1, FakeModel(0.02)))
        assert j.calls == 1 and r.decided_by == "llm"
        assert r.prefilter_score == pytest.approx(0.02)

    async def test_판단할_내용_없으면_모델_없어도_통과(self):
        j = CountingJudge()
        r = await process([], j, "ㅋㅋㅋㅋ", prefilter=Prefilter("on", 0.1, None))
        assert j.calls == 0 and r.destination == PASSED

    async def test_모델_파일_없으면_GPT로(self):
        j = CountingJudge()
        r = await process([], j, "영상 재밌네요", prefilter=Prefilter("on", 0.1, None))
        assert j.calls == 1 and r.prefilter_score is None

    async def test_검토어에_걸린_건_앞단이_못_넘김(self):
        """관리자가 보겠다고 등록한 단어다. ML 이 정상이라 해도 GPT·관리자에게 간다."""
        j = CountingJudge()
        r = await process([review_rule("진용진")], j, "진용진 영상 재밌네요",
                          prefilter=Prefilter("on", 0.1, FakeModel(0.01)))
        assert j.calls == 1
        assert r.destination == QUEUE_INFO

    async def test_prefilter_없으면_예전과_같다(self):
        j = CountingJudge()
        r = await process([], j, "ㅋㅋㅋㅋ")
        assert j.calls == 1 and r.prefilter_score is None
