"""라벨링 작업을 배정한다 — 누가 어느 댓글을 볼지를 label_tasks 에 넣는다.

CSV 시트(make_labeling.py) 대신 화면(/label)으로 한다. 시트와 다른 점:
사람이 버튼을 누르는 시각이 남아서 '댓글 하나 판단에 몇 초' 가 실측된다.

배정 방식
  - 뽑는 곳: 최신 판정이 검토 큐(queue_judge / queue_info)인 댓글이 기본.
    --passed-ratio 만큼은 통과분에서 무작위로 섞는다. 통과분이 0 이면
    'AI 가 그냥 통과시킨 악플' 을 영원히 못 잰다.
  - 공통 구간: --common 건은 모든 사람이 똑같이 본다. 사람끼리 얼마나 갈리는지
    재는 용도다. 각자 목록의 맨 앞에 둔다 — 중간에 그만둬도 일치도는 나오게.
  - 나머지는 겹치지 않게 나눈다.
  - AI 판정은 여기서 읽기만 한다 (어디서 뽑을지 정하려고). 표에는 안 들어간다.

    python -m scripts.make_label_tasks --people 박승민 김지황 박정호 조민준
    python -m scripts.make_label_tasks --people A B --per-person 500 --common 200 --passed-ratio 0.2
    python -m scripts.make_label_tasks ... --channels 4 1 2 1474
    python -m scripts.make_label_tasks ... --reset      기존 배정을 지우고 다시

DB 에 쓴다. 이미 배정이 있으면 --reset 없이는 멈춘다.
"""

import argparse
import asyncio
import random
import sys

from sqlalchemy import text

from app.db.session import AsyncSessionLocal, engine
from app.services.retention import MARK

DDL = """
CREATE TABLE IF NOT EXISTS label_tasks (
    id          SERIAL PRIMARY KEY,
    labeler     VARCHAR(50) NOT NULL,
    comment_id  INTEGER NOT NULL REFERENCES comments(id),
    position    INTEGER NOT NULL,
    segment     VARCHAR(10) NOT NULL,
    source      VARCHAR(10) NOT NULL,
    label       VARCHAR(10),
    labeled_at  TIMESTAMP,
    seconds     DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS ix_label_tasks_labeler ON label_tasks (labeler);
CREATE INDEX IF NOT EXISTS ix_label_tasks_comment_id ON label_tasks (comment_id);
"""

POOL = """
    SELECT c.id, ra.destination
    FROM comments c
    JOIN LATERAL (
        SELECT destination FROM risk_assessments r
        WHERE r.comment_id = c.id ORDER BY r.id DESC LIMIT 1
    ) ra ON true
    WHERE c.channel_id = ANY(:channels)
      AND c.content IS NOT NULL AND length(trim(c.content)) > 0
      AND c.content <> :mark
"""


async def main() -> None:
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser()
    ap.add_argument("--people", nargs="+", required=True)
    ap.add_argument("--per-person", type=int, default=500)
    ap.add_argument("--common", type=int, default=200)
    ap.add_argument("--passed-ratio", type=float, default=0.2,
                    help="통과분 비율. 0 이면 큐에서만")
    ap.add_argument("--channels", type=int, nargs="+", default=[4, 1, 2, 1474])
    ap.add_argument("--seed", type=int, default=2026)
    ap.add_argument("--reset", action="store_true")
    a = ap.parse_args()

    if a.common > a.per_person:
        raise SystemExit("[FAIL] 공통이 1인 배정보다 클 수 없다")
    rnd = random.Random(a.seed)

    async with AsyncSessionLocal() as db:
        for stmt in DDL.strip().split(";"):
            if stmt.strip():
                await db.execute(text(stmt))
        await db.commit()

        있음 = (await db.execute(text("SELECT count(*) FROM label_tasks"))).scalar_one()
        if 있음 and not a.reset:
            raise SystemExit(
                f"[FAIL] 이미 {있음:,}건 배정돼 있다. 지우고 다시 하려면 --reset"
            )
        if 있음 and a.reset:
            답함 = (await db.execute(
                text("SELECT count(*) FROM label_tasks WHERE label IS NOT NULL"))).scalar_one()
            if 답함:
                print(f"  주의: 이미 답한 {답함:,}건이 함께 지워진다.")
            await db.execute(text("DELETE FROM label_tasks"))
            await db.commit()

        rows = (await db.execute(text(POOL), {"channels": a.channels, "mark": MARK})).all()

    큐 = [cid for cid, d in rows if d in ("queue_judge", "queue_info")]
    통과 = [cid for cid, d in rows if d == "passed"]
    rnd.shuffle(큐)
    rnd.shuffle(통과)

    n_people = len(a.people)
    개별 = a.per_person - a.common
    총 = a.common + 개별 * n_people
    n_통과 = round(총 * a.passed_ratio)
    n_큐 = 총 - n_통과
    if n_큐 > len(큐) or n_통과 > len(통과):
        raise SystemExit(
            f"[FAIL] 모자란다. 필요 큐 {n_큐} / 있음 {len(큐)}, 필요 통과 {n_통과} / 있음 {len(통과)}"
        )

    # 큐·통과를 비율대로 섞은 뒤 공통 → 개별 순으로 잘라 쓴다.
    풀 = [(c, "큐") for c in 큐[:n_큐]] + [(c, "통과") for c in 통과[:n_통과]]
    rnd.shuffle(풀)
    공통 = 풀[:a.common]
    나머지 = 풀[a.common:]

    작업 = []
    for i, 사람 in enumerate(a.people):
        내것 = list(공통)
        rnd.shuffle(내것)               # 공통이라도 사람마다 순서는 다르게
        개별분 = 나머지[i * 개별:(i + 1) * 개별]
        rnd.shuffle(개별분)
        for pos, (cid, src) in enumerate(내것 + 개별분, 1):
            작업.append({
                "labeler": 사람, "comment_id": cid, "position": pos,
                "segment": "공통" if pos <= a.common else "개별", "source": src,
            })

    async with AsyncSessionLocal() as db:
        await db.execute(
            text("""INSERT INTO label_tasks (labeler, comment_id, position, segment, source)
                    VALUES (:labeler, :comment_id, :position, :segment, :source)"""),
            작업,
        )
        await db.commit()
    await engine.dispose()

    print(f"[OK] {len(작업):,}건 배정")
    print(f"     사람 {n_people}명 × {a.per_person}건 (공통 {a.common} + 개별 {개별})")
    print(f"     댓글 {총:,}건 = 큐 {n_큐:,} + 통과 {n_통과:,}  (채널 {a.channels})")
    print(f"     화면: http://localhost:8000/label")


if __name__ == "__main__":
    asyncio.run(main())
