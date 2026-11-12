"""수집한 댓글을 파이프라인에 통과시키고 관리자 화면처럼 보여준다.

채널 설정(등록어·맥락)은 전부 DB 에서 읽는다. 코드에 채널 이름이나 단어를
박아두지 않는다 — 실제 서비스에서는 관리자가 화면에서 등록할 것들이다.

    python -m scripts.run_pipeline                     채널 목록 보기
    python -m scripts.run_pipeline --channel 1         id 로 지정
    python -m scripts.run_pipeline --channel 진용진      채널명 일부로 지정
    python -m scripts.run_pipeline --channel 1 --dry   호출 없이 등록어 단계만
    python -m scripts.run_pipeline --channel 1 --n 50  일부만 (비용 확인용)
    python -m scripts.run_pipeline --channel 1 --save  결과를 DB 에 저장
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from sqlalchemy import select
from sqlalchemy import text as sq

from app.core.config import get_settings
from app.db.models import Channel, ChannelRule
from app.db.session import AsyncSessionLocal, engine
from app.services.llm import LlmJudge, VideoContext
from app.services.pipeline import HIDDEN, PASSED, QUEUE_INFO, QUEUE_JUDGE, process_many
from app.services.prefilter import get_prefilter
from app.services.auto_hide import apply_auto_hides
from app.services.store import save_results

OUT_DIR = Path(os.environ.get("CLAUDE_SCRATCHPAD", ".")) / "eval"

LABEL = {
    HIDDEN: ("숨김 목록", "자동으로 가려진 것 — 오등록을 발견하는 유일한 통로"),
    QUEUE_JUDGE: ("검토 큐 · 판단 필요", "AI가 확정하지 못한 것"),
    QUEUE_INFO: ("검토 큐 · 참고", "AI는 정상이지만 등록어가 나온 것"),
    PASSED: ("통과", "그대로 공개된 것 — 놓친 게 없는지 확인용"),
}


async def list_channels(db) -> None:
    rows = (await db.execute(sq("""
        SELECT ch.id, ch.channel_title,
               (SELECT count(*) FROM comments c WHERE c.channel_id = ch.id) AS n,
               (SELECT count(*) FROM channel_rules r
                 WHERE r.channel_id = ch.id AND r.enabled) AS rules,
               (ch.context IS NOT NULL) AS has_ctx
        FROM channels ch ORDER BY ch.id
    """))).all()
    if not rows:
        print("채널이 없다. 먼저 수집해야 한다:")
        print('  python -m scripts.collect_channel "@채널핸들" 10')
        return
    print(f"{'id':>3}  {'채널':<20}{'댓글':>7}{'등록어':>7}  맥락")
    print("-" * 52)
    for cid, title, n, rules, ctx in rows:
        print(f"{cid:>3}  {str(title)[:18]:<20}{n:>7}{rules:>7}  {'있음' if ctx else '없음'}")
    print("\n  python -m scripts.run_pipeline --channel <id>")


async def find_channel(db, key: str) -> Channel | None:
    """id 숫자 또는 채널명 일부로 찾는다."""
    if key.isdigit():
        return await db.get(Channel, int(key))
    rows = (await db.execute(
        select(Channel).where(Channel.channel_title.ilike(f"%{key}%"))
    )).scalars().all()
    if len(rows) > 1:
        raise SystemExit("[FAIL] 이름이 여러 채널에 걸린다: "
                         + ", ".join(f"{c.id}={c.channel_title}" for c in rows))
    return rows[0] if rows else None


async def load_comments(db, channel_id: int, limit: int | None, only_new: bool = False):
    """(id, 본문, 부모본문) 목록. id 는 판별 결과를 되돌려 붙일 때 쓴다.

    only_new 는 아직 판별하지 않은 것(status='pending')만 고른다. 주기적으로
    재수집할 때 이미 판별한 댓글을 다시 LLM 에 보내지 않으려는 것 —
    같은 값에 돈을 두 번 쓸 이유가 없다.
    """
    rows = (await db.execute(sq(f"""
        SELECT c.id, c.content, p.content AS parent,
               v.title AS v_title, v.context AS v_memo
        FROM comments c
        LEFT JOIN comments p ON p.youtube_comment_id = c.parent_comment_id
        LEFT JOIN videos v ON v.video_id = c.video_id
        WHERE c.channel_id = :cid {"AND c.status = 'pending'" if only_new else ""}
        ORDER BY c.id
    """), {"cid": channel_id})).all()
    return [
        (r[0], r[1], r[2], VideoContext(title=r[3], memo=r[4]))
        for r in rows
    ][: limit or None]


def show(results) -> None:
    total = len(results)
    buckets = {k: [r for r in results if r.destination == k] for k in LABEL}

    print("\n" + "=" * 66)
    print("관리자 화면")
    print("=" * 66)
    for key, (name, _) in LABEL.items():
        n = len(buckets[key])
        print(f"  {name:<22}{n:>5}건 ({n/total:5.1%})  {'#' * round(n / total * 40)}")
    q = len(buckets[QUEUE_JUDGE]) + len(buckets[QUEUE_INFO])
    print(f"\n  -> 관리자가 실제로 여는 화면: {q}건 ({q/total:.1%})")

    for key, (name, note) in LABEL.items():
        print("\n" + "-" * 66)
        print(f"[{name}] {note}")
        print("-" * 66)
        rows = buckets[key]
        if not rows:
            print("  (없음)")
        for r in rows[:8]:
            src = f"규칙 '{r.rule_value}'" if r.decided_by == "rule" else (r.llm_category or "")
            head = f"  [{src}] " if src else "  "
            print(f"{head}{' '.join(r.text.split())[:48]}")
            if r.reason and key != PASSED:
                print(f"      {r.reason[:56]}")


async def main() -> None:
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", help="channels.id 또는 채널명 일부")
    ap.add_argument("--n", type=int, default=None)
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--new", action="store_true",
                    help="아직 판별하지 않은 댓글만 (주기 수집용)")
    ap.add_argument("--save", action="store_true",
                    help="판별 결과를 DB(risk_assessments, comments.status)에 저장")
    args = ap.parse_args()

    async with AsyncSessionLocal() as db:
        if not args.channel:
            await list_channels(db)
            await engine.dispose()
            return

        ch = await find_channel(db, args.channel)
        if ch is None:
            print(f"[FAIL] 채널을 못 찾았다: {args.channel}\n")
            await list_channels(db)
            await engine.dispose()
            return

        # 등록어와 채널 맥락은 DB 에서 읽는다. 없으면 없는 대로 돈다.
        rules = (await db.execute(
            select(ChannelRule).where(
                ChannelRule.channel_id == ch.id, ChannelRule.enabled.is_(True))
        )).scalars().all()
        items = await load_comments(db, ch.id, args.n, only_new=args.new)
        title, ctx = ch.channel_title, ch.context or ""
        auto_hide = ch.auto_hide_set

        # AI 자동 판별에는 관리자 명시 동의가 필요하다 (YouTube API 정책).
        # 동의 칸을 만들어두고 확인하지 않으면 정책이 문서에만 있는 셈이 된다.
        if not ch.ai_consent_agreed:
            raise SystemExit(
                f"[FAIL] 채널 {ch.id} 은 AI 판별 동의를 받지 않았다.\n"
                "  관리자 화면 > 채널 관리에서 동의하거나,\n"
                f"  python -m scripts.consent --channel {ch.id} --agree"
            )

    by_action: dict[str, int] = {}
    for r in rules:
        by_action[r.action] = by_action.get(r.action, 0) + 1

    print(f"채널: {title} (id={ch.id})")
    print(f"댓글 {len(items)}건 "
          f"(답글 {sum(1 for _, _, p, _ in items if p)}건은 부모와 함께 판단)")
    print("등록어: " + (" · ".join(f"{a} {n}개" for a, n in sorted(by_action.items()))
                       or "없음 (channel_rules 비어 있음)"))
    print(f"채널 맥락: {str(len(ctx)) + '자' if ctx else '없음'}")
    print("자동 숨김: " + (" · ".join(sorted(auto_hide)) or "없음 (전부 검토 큐로)"))

    if not items:
        print("새로 판별할 댓글이 없다." if args.new else "댓글이 없다. 먼저 수집해야 한다.")
        await engine.dispose()
        return

    if args.dry:
        from app.services.moderation import judge_rules
        c = {"block": 0, "review": 0, "pass": 0}
        for _, t, _, _ in items:
            c[judge_rules(rules, t).verdict] += 1
        n = c["review"] + c["pass"]
        print(f"\n등록어 단계만: 차단 {c['block']} / 검토 {c['review']} / 나머지 {c['pass']}")
        print(f"LLM 호출 예정: {n}건 (약 {n * 0.17:.0f}원)")
        await engine.dispose()
        return

    judge = LlmJudge(concurrency=8, max_calls=len(items) + 10, channel_context=ctx,
                     prompt_template=ch.prompt_template)
    print("판별 중...")
    results = await process_many(
        rules, judge,
        [(t, p) for _, t, p, _ in items],
        auto_hide,
        [v for *_, v in items],
        prefilter=get_prefilter(),
    )

    if args.save:
        pairs = [(cid, r) for (cid, *_), r in zip(items, results)]
        async with AsyncSessionLocal() as db:
            n = await save_results(
                db, pairs,
                model=get_settings().openai_model,
                prompt_version=judge.prompt_version,
            )
            # 숨김 행선지(차단어·자동 숨김 분류)는 유튜브에도 반영한다. watcher 와 같다.
            hides = await apply_auto_hides(db, ch, pairs)
        print(f"DB 저장 {n}건 (risk_assessments + comments.status)")
        if hides.attempted:
            print(f"자동 숨김 {hides.attempted}건 → 유튜브 반영 {hides.synced} · 실패 {hides.failed} · 미연동 {hides.not_connected}")
    else:
        print("* DB 에 저장하지 않았다. 저장하려면 --save")

    st = judge.stats
    print(f"\nLLM 호출 {st.calls}건 / 실패 {st.errors}건")
    print(f"캐시 적중 {st.cached_tokens:,} 토큰 / 비용 ${st.cost_usd:.4f}"
          f" (약 {st.cost_usd * 1400:.0f}원)")
    show(results)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"pipeline_run_ch{ch.id}.json"
    out.write_text(json.dumps([{
        "text": r.text, "destination": r.destination, "decided_by": r.decided_by,
        "rule_value": r.rule_value, "rule_action": r.rule_action,
        "llm_label": r.llm_label, "llm_category": r.llm_category, "reason": r.reason,
    } for r in results], ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n전체 결과: {out}")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
