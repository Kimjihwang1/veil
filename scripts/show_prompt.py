"""LLM 이 실제로 받는 프롬프트를 그대로 보여준다.

코드 여기저기 흩어진 문자열을 읽어서 머릿속으로 조립할 일이 아니다.
무엇을 시키고 있는지는 완성된 형태로 봐야 보인다.

    python -m scripts.show_prompt              공통 프롬프트만
    python -m scripts.show_prompt 4            채널 4 의 기준까지 붙여서
    python -m scripts.show_prompt 4 > 프롬프트.txt
"""

import asyncio
import sys

from app.db.models import Channel
from app.db.session import AsyncSessionLocal, engine
from app.services.llm import build_prompt, prompt_version


async def main() -> None:
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace")

    맥락, 이름 = "", "(채널 기준 없음 — 공통 프롬프트만)"
    if len(sys.argv) > 1:
        async with AsyncSessionLocal() as db:
            ch = await db.get(Channel, int(sys.argv[1]))
            if ch is None:
                raise SystemExit(f"[FAIL] 채널 {sys.argv[1]} 없음")
            맥락 = ch.context or ""
            이름 = f"{ch.channel_title} (채널 {ch.id})"
        await engine.dispose()

    본문 = build_prompt(맥락)
    print("=" * 74)
    print(f" {이름}")
    print(f" 프롬프트 판 {prompt_version(맥락)} · {len(본문):,}자")
    print("=" * 74)
    print(본문)
    print("=" * 74)
    print(" 이 아래로 댓글 한 건씩이 사용자 메시지로 들어간다.")
    print(" 영상 제목·설명 같은 영상별 정보도 거기 붙는다 (앞부분 캐시 유지).")


if __name__ == "__main__":
    asyncio.run(main())
