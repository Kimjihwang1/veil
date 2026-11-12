"""LLM 호출 재시도.

재시도가 없던 탓에 채널 4 재판별에서 1,103건 중 194건(17.6%)이 통째로
실패했다. 실패한 건은 근거 없이 검토 큐에 쌓여 관리자를 헷갈리게 한다.
같은 일이 조용히 되풀이되지 않도록 고정해둔다.
"""

import httpx
import pytest
from openai import APIConnectionError, APITimeoutError, RateLimitError

from app.services.llm import RETRIES, LlmJudge
from app.services.pipeline import QUEUE_JUDGE, process

pytestmark = pytest.mark.asyncio


def _rate_limit() -> RateLimitError:
    req = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    resp = httpx.Response(429, request=req)
    return RateLimitError("rate limited", response=resp, body=None)


class FakeCompletions:
    """앞의 몇 번은 실패하고 그다음 성공하는 가짜 API."""

    def __init__(self, fail_times: int, error=None):
        self.fail_times = fail_times
        self.error = error or _rate_limit()
        self.calls = 0

    async def create(self, **kw):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.error

        class Msg:
            content = (
                '{"label":"safe","category":"정상","confidence":0.9,"reason":"괜찮다"}'
            )

        class Choice:
            message = Msg()

        class Usage:
            prompt_tokens = 100
            completion_tokens = 10
            prompt_tokens_details = None

        class R:
            choices = [Choice()]
            usage = Usage()

        return R()


def judge_with(fake) -> LlmJudge:
    j = LlmJudge.__new__(LlmJudge)          # __init__ 은 API 키를 요구한다
    from app.services.llm import LlmStats
    import asyncio

    j._client = type("C", (), {"chat": type("Ch", (), {"completions": fake})()})()
    j._model = "test-model"
    j._sem = asyncio.Semaphore(4)
    j._max_calls = 100
    j._prompt = "prompt"
    j.stats = LlmStats()
    return j


class TestRetry:
    async def test_recovers_from_rate_limit(self, monkeypatch):
        monkeypatch.setattr("asyncio.sleep", lambda *_: _noop())
        fake = FakeCompletions(fail_times=2)
        j = judge_with(fake)

        v = await j.judge("테스트 댓글")

        assert v.error is None, "재시도했으면 성공했어야 한다"
        assert v.label == "safe"
        assert fake.calls == 3          # 실패 2 + 성공 1
        assert j.stats.retries == 2
        assert j.stats.errors == 0

    async def test_gives_up_after_limit(self, monkeypatch):
        monkeypatch.setattr("asyncio.sleep", lambda *_: _noop())
        fake = FakeCompletions(fail_times=99)
        j = judge_with(fake)

        v = await j.judge("테스트 댓글")

        assert v.error is not None
        assert fake.calls == RETRIES     # 무한정 매달리지 않는다
        assert j.stats.errors == 1

    @pytest.mark.parametrize(
        "err",
        [APITimeoutError(httpx.Request("POST", "http://x")),
         APIConnectionError(request=httpx.Request("POST", "http://x"))],
    )
    async def test_other_transient_errors_retry(self, monkeypatch, err):
        monkeypatch.setattr("asyncio.sleep", lambda *_: _noop())
        fake = FakeCompletions(fail_times=1, error=err)
        j = judge_with(fake)

        assert (await j.judge("테스트")).error is None
        assert fake.calls == 2

    async def test_permanent_error_is_not_retried(self, monkeypatch):
        """잘못된 요청은 다시 걸어도 똑같다. 돈과 시간만 쓴다."""
        monkeypatch.setattr("asyncio.sleep", lambda *_: _noop())
        fake = FakeCompletions(fail_times=99, error=ValueError("bad request"))
        j = judge_with(fake)

        v = await j.judge("테스트")

        assert v.error is not None
        assert fake.calls == 1


class TestFailureIsRecorded:
    async def test_reason_explains_the_failure(self, monkeypatch):
        """실패 사유가 남아야 나중에 원인을 찾을 수 있다."""
        monkeypatch.setattr("asyncio.sleep", lambda *_: _noop())
        j = judge_with(FakeCompletions(fail_times=99))

        r = await process([], j, "테스트 댓글")

        assert r.destination == QUEUE_JUDGE      # 실패건은 사람이 본다
        assert "판별 실패" in r.reason
        assert r.reason != ""


async def _noop():
    return None
