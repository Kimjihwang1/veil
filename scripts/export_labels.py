"""화면(/label)에서 누른 답을 채점기가 읽는 시트 형식으로 내보낸다.

score_labeling.py 는 CSV 시트(make_labeling.py 가 만들던 것)를 읽는다. 화면으로
바꿨어도 채점 코드를 안 고치려고, 같은 열·같은 값으로 뽑는다.

    id, 구간, 부모댓글, 댓글, 판정, 유형, 메모
    판정: 유해 / 애매 / 정상   (화면의 hide / unsure / keep)

    python -m scripts.export_labels                 labeling_live/<사람>.csv 로
    python -m scripts.export_labels --dir 어딘가
    python -m scripts.score_labeling --dir labeling_live   그다음 채점

덤으로 사람별 '댓글 하나에 몇 초' 를 찍는다. 시트로는 절대 못 재던 숫자다.
"""

import argparse
import asyncio
import csv
import io
import statistics
import sys
from pathlib import Path

from sqlalchemy import text

from app.db.session import AsyncSessionLocal, engine

판정 = {"hide": "유해", "unsure": "애매", "keep": "정상"}

Q = """
    SELECT t.labeler, t.comment_id, t.segment, t.label, t.seconds,
           c.content, p.content AS parent
    FROM label_tasks t
    JOIN comments c ON c.id = t.comment_id
    LEFT JOIN comments p ON p.youtube_comment_id = c.parent_comment_id
    WHERE t.label IS NOT NULL
    ORDER BY t.labeler, t.position
"""


async def main() -> None:
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="labeling_live")
    a = ap.parse_args()

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(text(Q))).all()
    await engine.dispose()

    if not rows:
        raise SystemExit("[FAIL] 아직 답한 게 없다.")

    out = Path(a.dir)
    out.mkdir(exist_ok=True)
    사람별: dict[str, list] = {}
    for r in rows:
        사람별.setdefault(r.labeler, []).append(r)

    print(f"  {'사람':<8} {'답한 건수':>8} {'초/건 중앙값':>12}")
    for 사람, rs in 사람별.items():
        with io.open(out / f"{사람}.csv", "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["id", "구간", "부모댓글", "댓글", "판정", "유형", "메모"])
            for r in rs:
                w.writerow([r.comment_id, r.segment, r.parent or "",
                            r.content, 판정[r.label], "", ""])
        초들 = [r.seconds for r in rs if r.seconds is not None and 0 < r.seconds < 300]
        중앙 = f"{statistics.median(초들):.1f}초" if 초들 else "-"
        print(f"  {사람:<8} {len(rs):>8,} {중앙:>12}")

    print(f"\n[OK] {out}/ 에 {len(사람별)}명 시트. 채점: python -m scripts.score_labeling --dir {out}")


if __name__ == "__main__":
    asyncio.run(main())
