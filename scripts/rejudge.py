"""이미 판별한 댓글을 지금 프롬프트로 다시 물어본다.

프롬프트를 고치면 기존 판정은 저절로 안 바뀐다. DB 에 저장된 건 그때 그 기준의
결과이기 때문이다. 그렇다고 전량을 다시 돌리면 대부분은 원래 문제가 없던
것들이라 돈만 나간다. 그래서 구간을 골라 돌린다.

기본은 '유해 판정만' 이다 — 오탐은 거기 모여 있고, 정상 판정을 다시 보는 건
'놓친 것 찾기'라 성격이 다르다 (표본 200건에서 3.5%만 움직였고 그것도 노이즈였다).

판정 이력은 지우지 않고 새 행으로 쌓는다. 무엇이 언제 왜 바뀌었는지 남아야
나중에 관리자에게 설명할 수 있다.

    python -m scripts.rejudge --channel 4              유해 판정만 (기본)
    python -m scripts.rejudge --channel 4 --all        정상까지 전부
    python -m scripts.rejudge --channel 4 --dry        건수·비용만 확인
"""

import argparse
import asyncio
import collections
import sys

from sqlalchemy import select
from sqlalchemy import text as sq

from app.core.config import get_settings
from app.db.models import Channel, ChannelRule
from app.db.session import AsyncSessionLocal, engine
from app.services.llm import LlmJudge, VideoContext
from app.services.pipeline import process
from app.services.store import save_results

CR = chr(13)   # 같은 줄에 덮어쓰기 위한 캐리지리턴

SQL = """
SELECT c.id, c.content, p.content AS parent, ra.category,
       v.title AS v_title, v.context AS v_memo
FROM comments c
JOIN risk_assessments ra
  ON ra.id = (SELECT max(id) FROM risk_assessments WHERE comment_id = c.id)
LEFT JOIN comments p ON p.youtube_comment_id = c.parent_comment_id
LEFT JOIN videos v ON v.video_id = c.video_id
WHERE c.channel_id = :cid {cond}
ORDER BY c.id
"""


async def main() -> None:
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", type=int, required=True)
    ap.add_argument("--all", action="store_true", help="정상 판정까지 전부")
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    cond = "" if args.all else "AND ra.risk_level <> 'safe'"

    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(sq(SQL.format(cond=cond)), {"cid": args.channel})
        ).all()
        rules = (
            await db.execute(
                select(ChannelRule).where(
                    ChannelRule.channel_id == args.channel,
                    ChannelRule.enabled.is_(True),
                )
            )
        ).scalars().all()
        # 채널 맥락과 자동 숨김 설정. run_pipeline·watch 와 같은 기준으로
        # 돌아야 재판별 결과를 평소 판정과 견줄 수 있다.
        ch = await db.get(Channel, args.channel)
        if ch is None:
            raise SystemExit(f"[FAIL] 채널 {args.channel} 이 없다.")
        ctx, auto_hide = ch.context or "", ch.auto_hide_set

    if not rows:
        raise SystemExit("다시 돌릴 댓글이 없다.")

    before = collections.Counter(r[3] or "(없음)" for r in rows)
    print(f"대상 {len(rows)}건 · 등록어 {len(rules)}개")
    print("  전: " + " · ".join(f"{k} {v}" for k, v in before.most_common(8)))
    print("  채널 맥락: " + (f"{len(ctx)}자" if ctx else "없음"))
    print("  자동 숨김: " + (" · ".join(sorted(auto_hide)) or "없음 (전부 검토 큐로)"))
    print(f"  예상 비용 약 {len(rows) * 0.177:.0f}원")

    if args.dry:
        await engine.dispose()
        return

    judge = LlmJudge(
        concurrency=8, max_calls=len(rows) + 10, channel_context=ctx,
        prompt_template=ch.prompt_template,
    )

    total = len(rows)
    done = 0

    async def one(text, parent, video):
        nonlocal done
        r = await process(rules, judge, text, parent, auto_hide, video)
        done += 1
        if done % 10 == 0 or done == total:
            pct = done / total
            bar = "#" * round(pct * 24)
            print(CR + f"  {done:>5}/{total}  {pct:5.0%} |{bar:<24}| "
                  f"{judge.stats.cost_usd * 1400:.0f}원", end="", flush=True)
        return r

    res = list(await asyncio.gather(*(
        one(r[1], r[2], VideoContext(title=r[4], memo=r[5])) for r in rows
    )))
    print()

    async with AsyncSessionLocal() as db:
        n = await save_results(
            db,
            [(r[0], v) for r, v in zip(rows, res)],
            model=get_settings().openai_model,
            prompt_version=judge.prompt_version,
        )

    after = collections.Counter(v.llm_category or "(실패)" for v in res)
    dest = collections.Counter(v.destination for v in res)
    moved = sum(1 for r, v in zip(rows, res) if v.llm_category != r[3])
    st = judge.stats

    print("  후: " + " · ".join(f"{k} {v}" for k, v in after.most_common(8)))
    print(f"\n바뀐 것 {moved}건 ({moved / len(rows):.0%})")
    print("행선지: " + " · ".join(f"{k} {c}" for k, c in dest.most_common()))
    print(f"저장 {n}건 / 실패 {st.errors}건 / {st.cost_usd * 1400:.0f}원")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
