"""댓글을 벡터로 바꾼다 — 과거 유사 사례 검색용 (F_R_114).

왜 벡터인가: 관리자에게 보여줄 것은 '단어가 겹치는 댓글'이 아니라
'같은 뜻의 댓글'이다. "주소 알아내서 찾아간다"와 "어디 사는지 알아냈음"은
겹치는 단어가 없지만 관리자에겐 같은 사안이다. LIKE 검색으로는 못 잡는다.

값이 싸다. text-embedding-3-small 은 100만 토큰에 $0.02 —
댓글 1,100건이면 1원이 안 된다. 판별용 LLM 보다 200배 싸다.
"""

import asyncio
from dataclasses import dataclass

from openai import AsyncOpenAI, RateLimitError

from app.core.config import get_settings
from app.db.models import EMBEDDING_DIM

MODEL = "text-embedding-3-small"

# 한 번에 보낼 댓글 수. 요청 수를 줄이는 게 목적이라 넉넉히 잡는다.
BATCH = 128


@dataclass
class EmbedStats:
    calls: int = 0
    texts: int = 0
    tokens: int = 0

    @property
    def cost_usd(self) -> float:
        return self.tokens / 1_000_000 * 0.02


class Embedder:
    def __init__(self, api_key: str | None = None):
        self.client = AsyncOpenAI(api_key=api_key or get_settings().openai_api_key)
        self.stats = EmbedStats()

    async def _call(self, chunk: list[str]) -> list[list[float]]:
        # 빈 문자열은 API 가 거부한다. 자리를 지켜야 순서가 안 어긋난다.
        safe = [t.strip() or "." for t in chunk]
        for attempt in range(6):
            try:
                r = await self.client.embeddings.create(model=MODEL, input=safe)
                self.stats.calls += 1
                self.stats.texts += len(safe)
                self.stats.tokens += r.usage.total_tokens
                return [d.embedding for d in r.data]
            except RateLimitError:
                await asyncio.sleep(min(2**attempt, 60))
        raise RuntimeError("임베딩 레이트 리밋이 안 풀린다.")

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """입력 순서 그대로 벡터를 돌려준다."""
        out: list[list[float]] = []
        for i in range(0, len(texts), BATCH):
            out += await self._call(texts[i : i + BATCH])
        assert len(out) == len(texts)
        assert not out or len(out[0]) == EMBEDDING_DIM
        return out
