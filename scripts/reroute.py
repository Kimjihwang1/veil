"""정책을 바꿨을 때, 저장된 판정만으로 댓글을 다시 배치한다.

LLM 을 다시 부르지 않는다. 자동 숨김 대상 같은 '정책'은 판정과 별개라서,
정책만 바뀌면 이미 받아둔 label/category 로 행선지를 다시 계산하면 된다.
재판별은 돈도 들고 결과가 실행마다 조금씩 달라져서 비교가 안 된다.

    python -m scripts.reroute --channel 2          바뀌는 것만 보여준다
    python -m scripts.reroute --channel 2 --apply  실제로 DB 에 반영

관리자가 이미 처리한 댓글(reviewed_at)은 건드리지 않는다.
사람의 판단이 정책보다 우선한다.
"""

import argparse
import asyncio
import sys

from sqlalchemy import func, select, update

from app.db.models import Channel, Comment, RiskAssessment
from app.db.session import AsyncSessionLocal, engine
from app.services.pipeline import route
from app.services.store import STATUS


async def main() -> None:
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", type=int, required=True)
    ap.add_argument("--apply", action="store_true", help="실제로 DB 에 반영")
    args = ap.parse_args()

    async with AsyncSessionLocal() as db:
        # 이 채널이 켜둔 자동 숨김 분류. 정책은 코드가 아니라 채널이 들고 있다.
        ch = await db.get(Channel, args.channel)
        if ch is None:
            raise SystemExit(f"[FAIL] 채널 {args.channel} 이 없다.")
        auto_hide = ch.auto_hide_set

        # 댓글별 최신 판정만 본다 (재판별하면 행이 쌓인다).
        latest = (
            select(
                RiskAssessment.comment_id.label("cid"),
                func.max(RiskAssessment.id).label("rid"),
            )
            .group_by(RiskAssessment.comment_id)
            .subquery()
        )
        rows = (
            await db.execute(
                select(Comment, RiskAssessment)
                .join(latest, latest.c.cid == Comment.id)
                .join(RiskAssessment, RiskAssessment.id == latest.c.rid)
                .where(
                    Comment.channel_id == args.channel,
                    # 사람이 이미 본 것은 그대로 둔다.
                    Comment.reviewed_at.is_(None),
                )
            )
        ).all()

        if not rows:
            raise SystemExit(f"[FAIL] 채널 {args.channel} 에 재배치할 댓글이 없다.")

        moved: list[tuple[Comment, RiskAssessment, str]] = []
        for c, ra in rows:
            flagged = ra.rule_action == "review"
            dest = route(ra.risk_level or "safe", ra.category, flagged, auto_hide)
            if dest != ra.destination:
                moved.append((c, ra, dest))

        print("자동 숨김: " + (" · ".join(sorted(auto_hide)) or "없음 (전부 검토 큐로)"))
        print(f"채널 {args.channel} · 대상 {len(rows)}건 · 바뀌는 것 {len(moved)}건\n")

        if not moved:
            print("바뀌는 게 없다. 정책이 그대로거나 이미 반영됐다.")
            await engine.dispose()
            return

        # 어떤 이동이 얼마나 생기는지부터 보여준다
        summary: dict[tuple[str, str], int] = {}
        for _, ra, dest in moved:
            summary[(ra.destination or "?", dest)] = (
                summary.get((ra.destination or "?", dest), 0) + 1
            )
        for (src, dst), n in sorted(summary.items(), key=lambda x: -x[1]):
            print(f"  {src:<12} -> {dst:<12} {n:>4}건")

        print(f"\n{'-' * 66}\n숨김에서 풀리는 댓글 (관리자가 보게 된다)\n{'-' * 66}")
        released = [m for m in moved if m[1].destination == "hidden"]
        for c, ra, _ in released[:12]:
            print(f"  [{ra.category}] {' '.join(c.content.split())[:50]}")

        if not args.apply:
            print(f"\n반영하려면: python -m scripts.reroute --channel {args.channel} --apply")
            await engine.dispose()
            return

        # 판정 행은 지우지 않는다. 정책이 바뀐 시점의 새 판정으로 한 줄 더 쌓아,
        # 무엇이 언제 왜 바뀌었는지 남긴다.
        for c, ra, dest in moved:
            db.add(
                RiskAssessment(
                    comment_id=c.id,
                    stage=ra.stage,
                    destination=dest,
                    risk_level=ra.risk_level,
                    category=ra.category,
                    reasoning=ra.reasoning,
                    model=ra.model,
                    rule_id=ra.rule_id,
                    rule_value=ra.rule_value,
                    rule_action=ra.rule_action,
                )
            )

        by_status: dict[str, list[int]] = {}
        for c, _, dest in moved:
            by_status.setdefault(STATUS[dest], []).append(c.id)
        for status, ids in by_status.items():
            await db.execute(
                update(Comment)
                .where(Comment.id.in_(ids), Comment.reviewed_at.is_(None))
                .values(status=status)
            )

        await db.commit()
        print(f"\n반영 완료 {len(moved)}건 (판정 이력은 지우지 않고 새 행으로 쌓았다)")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
