"""프롬프트를 고쳤을 때 뭐가 깨졌는지 한 번에 본다.

프롬프트는 한쪽을 고치면 다른 쪽이 조용히 망가진다. 실제로 그랬다 —
우회 표기를 잡으려고 원칙을 고쳤더니 짧은 정상 댓글까지 올라올 뻔했고,
반대로 오탐을 줄이려다 욕설을 놓친 적도 있다. 눈으로 확인하는 방식으로는
매번 놓친다.

그래서 답을 미리 정해둔 댓글 묶음(eval/회귀세트.csv)을 놓고 채점한다.
정답은 라벨이 아니라 '관리자가 이걸 봐야 하는가' 하나로만 매긴다.
harmful 이냐 ambiguous 냐는 화면에서 어차피 같은 검토 큐로 간다.

    python -m scripts.check_prompt
    python -m scripts.check_prompt --channel 4     채널 기준까지 붙여서

다 맞으면 0, 하나라도 틀리면 1 로 끝난다.
"""

import argparse
import asyncio
import csv
import io
import sys
from pathlib import Path

from app.db.models import Channel
from app.db.session import AsyncSessionLocal, engine
from app.services.llm import LlmJudge, prompt_version

세트 = Path("eval/회귀세트.csv")

# 기대값 ↔ LLM 판정.
기대라벨 = {"통과": "safe", "애매": "ambiguous", "유해": "harmful"}
라벨기대 = {v: k for k, v in 기대라벨.items()}


def 행선지(값: str) -> str:
    """통과냐 아니냐가 먼저다.

    애매와 유해는 둘 다 검토 큐로 간다. 그래서 둘이 서로 바뀐 것은 틀림이
    아니라 경고로 둔다 — 세기가 다를 뿐 관리자는 어차피 그 댓글을 본다.
    반면 통과 ↔ 검토가 바뀌면 아무도 못 보거나 헛일이 생기므로 틀림이다.
    """
    return "통과" if 값 in ("통과", "safe") else "검토"


def 읽기() -> list[tuple[str, str, str]]:
    if not 세트.exists():
        raise SystemExit(f"[FAIL] {세트} 가 없다.")
    with io.open(세트, encoding="utf-8-sig", newline="") as f:
        return [(r["댓글"], r["기대"], r["왜"]) for r in csv.DictReader(f)]


async def main() -> None:
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", type=int, default=None,
                    help="이 채널의 기준까지 붙여서 채점한다")
    a = ap.parse_args()

    맥락, 이름 = "", "공통 프롬프트"
    if a.channel:
        async with AsyncSessionLocal() as db:
            ch = await db.get(Channel, a.channel)
            if ch is None:
                raise SystemExit(f"[FAIL] 채널 {a.channel} 없음")
            맥락, 이름 = ch.context or "", f"{ch.channel_title} 기준 포함"

    행 = 읽기()
    print(f"  {이름} · 판 {prompt_version(맥락)} · {len(행)}건\n")

    j = LlmJudge(concurrency=8, channel_context=맥락)
    판정 = await asyncio.gather(*(j.judge(t) for t, _, _ in 행))

    틀림, 경고 = [], []
    for (글, 기대, 왜), v in zip(행, 판정):
        실제 = 라벨기대.get(v.label, v.label or "판별실패")
        if 실제 == 기대:
            continue
        묶음 = 틀림 if 행선지(기대) != 행선지(실제) else 경고
        묶음.append((글, 기대, 실제, 왜, v.category, v.reason))

    맞 = len(행) - len(틀림) - len(경고)
    꼬리 = f" · 세기만 다름 {len(경고)}건" if 경고 else ""
    print(f"  맞춤 {맞}/{len(행)}{꼬리}")

    def 찍기(제목, 목록):
        if not 목록:
            return
        print(f"\n  {제목}")
        for 글, 기대, 실제, 왜, 분류, 이유 in 목록:
            print(f"    [{기대} 여야 하는데 {실제}] {글[:46]}")
            print(f"        왜 넣었나 : {왜}")
            print(f"        LLM      : {분류} — {(이유 or '')[:50]}")

    찍기("틀린 것 — 행선지가 다르다", 틀림)
    찍기("경고 — 검토 큐로 가긴 하는데 세기가 다르다", 경고)
    print(f"\n  비용 약 {j.stats.cost_usd * 1400:.0f}원")

    await engine.dispose()
    raise SystemExit(0 if not 틀림 else 1)


if __name__ == "__main__":
    asyncio.run(main())
