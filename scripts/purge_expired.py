"""보관기한이 지난 댓글의 원문·작성자 정보를 지운다.

YouTube API 정책상 댓글 원문과 작성자 정보는 최대 30일만 보관할 수 있다.
그 뒤에는 위험도 같은 파생 지표만 남겨야 한다.

지금까지 retention_expires_at 을 채우기만 하고 아무도 지우지 않았다.
정책이 문서와 컬럼에만 있고 실행하는 주체가 없던 셈이다.

**행을 지우지 않고 비운다.** 행까지 지우면 "이 채널에서 몇 건을 처리했다"
같은 통계가 과거로 소급해 바뀐다. 관리자가 어제 본 숫자와 오늘 숫자가
달라지면 그게 더 이상하다. 그래서 개인정보만 지우고 뼈대는 남긴다.

    python -m scripts.purge_expired --dry     지울 건수만 본다
    python -m scripts.purge_expired           실제로 지운다
    python -m scripts.purge_expired --days 7  기한을 다르게 보고 싶을 때
"""

import argparse
import asyncio
import sys

from sqlalchemy import text as sq

from app.db.session import AsyncSessionLocal, engine

# 지울 대상과 지운 뒤 들어갈 값.
# 원문 자리에 빈칸이 아니라 표식을 남긴다 — 화면에서 "수집 실패"와
# "기한이 지나 지움"을 구분할 수 있어야 한다.
비움 = "(보관기한 경과로 삭제됨)"

셀것 = """
SELECT count(*) AS 대상,
       count(*) FILTER (WHERE content <> :mark) AS 아직_남은것,
       min(retention_expires_at) AS 가장_오래된_기한
FROM comments
WHERE retention_expires_at IS NOT NULL
  AND retention_expires_at < now() - make_interval(days => :grace)
"""

지울것 = """
UPDATE comments SET
    content = :mark,
    author_name = NULL,
    author_channel_id = NULL,
    embedding = NULL
WHERE retention_expires_at IS NOT NULL
  AND retention_expires_at < now() - make_interval(days => :grace)
  AND content <> :mark
"""

# 판정 근거에도 댓글 내용이 그대로 드러난다.
# ("여성 집단을 비하하는 표현이다" 같은 문장은 원문을 되짚어준다)
근거지울것 = """
UPDATE risk_assessments SET reasoning = NULL
WHERE comment_id IN (
    SELECT id FROM comments WHERE content = :mark
) AND reasoning IS NOT NULL
"""

이력지울것 = """
UPDATE actions SET note = NULL
WHERE comment_id IN (
    SELECT id FROM comments WHERE content = :mark
) AND note IS NOT NULL
"""

# 고소용 증거 (2026-11-09). 서버가 매일 도는 것(services/retention.py)과 같은 규칙.
증거지울것 = """
DELETE FROM evidence
WHERE expires_at IS NOT NULL
  AND expires_at < now() - make_interval(days => :grace)
"""


async def main() -> None:
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="건수만 보고 지우지 않는다")
    ap.add_argument(
        "--grace", type=int, default=0,
        help="기한이 지나고 며칠 더 두고 볼지 (기본 0 = 기한이 지나면 바로)",
    )
    args = ap.parse_args()
    p = {"mark": 비움, "grace": args.grace}

    async with AsyncSessionLocal() as db:
        r = (await db.execute(sq(셀것), p)).first()
        print(f"기한이 지난 댓글      {r[0]:,}건")
        print(f"그중 아직 안 지운 것   {r[1]:,}건")
        print(f"가장 오래된 기한      {r[2]}")

        if r[1] == 0:
            print("\n지울 것이 없다.")
            await engine.dispose()
            return

        if args.dry:
            print(f"\n실제로 지우려면 --dry 를 빼고 다시 실행한다.")
            await engine.dispose()
            return

        본문 = (await db.execute(sq(지울것), p)).rowcount
        근거 = (await db.execute(sq(근거지울것), p)).rowcount
        메모 = (await db.execute(sq(이력지울것), p)).rowcount
        증거 = (await db.execute(sq(증거지울것), {"grace": args.grace})).rowcount
        await db.commit()

        print(f"\n지움:")
        print(f"  댓글 원문·작성자·임베딩   {본문:,}건")
        print(f"  판정 근거 문장            {근거:,}건")
        print(f"  조치 메모                 {메모:,}건")
        print(f"  고소용 증거               {증거:,}건")
        print(f"\n판정 결과(위험도·카테고리)와 통계는 그대로 남는다.")

    await engine.dispose()


asyncio.run(main())
