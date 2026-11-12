"""보관기한이 지난 개인정보를 주기적으로 지운다.

YouTube API 정책상 댓글 원문·작성자는 최대 30일이다. 손으로 돌리는 스크립트
(scripts/purge_expired.py)만 있으면 누군가 잊는 순간 정책이 깨진다. 그래서
서버가 떠 있는 동안 스스로 돌게 한다.

별도 스케줄러(cron, Celery)를 붙이지 않은 이유: 지금은 서버가 한 대고,
하는 일이 UPDATE 몇 줄이다. 도구를 하나 더 늘리면 배포와 운영이 그만큼
복잡해진다. 서버가 여러 대가 되면 그때 옮긴다 — 그때는 여러 대가 동시에
같은 행을 지우려 드는 것도 같이 생각해야 한다.
"""

import asyncio
import logging

from sqlalchemy import text as sq

from app.db.session import AsyncSessionLocal

log = logging.getLogger("outlier.retention")

# 지운 자리에 남기는 표식. 빈 문자열로 두면 '수집 실패'와 구분이 안 된다.
MARK = "(보관기한 경과로 삭제됨)"

# 하루에 한 번. 기한이 몇 시간 늦게 지워지는 건 문제가 안 되고,
# 자주 돌아봐야 DB 만 긁는다.
INTERVAL = 24 * 60 * 60

# 서버가 막 떴을 때는 다른 일이 몰린다. 조금 기다렸다 시작한다.
FIRST_DELAY = 60

SQL = {
    "본문": """
        UPDATE comments SET
            content = :mark, author_name = NULL,
            author_channel_id = NULL, embedding = NULL
        WHERE retention_expires_at IS NOT NULL
          AND retention_expires_at < now()
          AND content <> :mark
    """,
    # 판정 근거에도 원문이 드러난다.
    # ("여성 집단을 비하하는 표현이다" 는 원문을 되짚어준다)
    "근거": """
        UPDATE risk_assessments SET reasoning = NULL
        WHERE reasoning IS NOT NULL
          AND comment_id IN (SELECT id FROM comments WHERE content = :mark)
    """,
    "메모": """
        UPDATE actions SET note = NULL
        WHERE note IS NOT NULL
          AND comment_id IN (SELECT id FROM comments WHERE content = :mark)
    """,
    # 고소용 증거 (2026-11-09). 원문·작성자가 전부라 지울 것만 남기는 게 아니라 행째 지운다.
    # 남기려면 관리자가 기한 안에 내려받아야 한다 — 화면에 남은 날짜를 띄운다.
    "증거": """
        DELETE FROM evidence
        WHERE expires_at IS NOT NULL AND expires_at < now()
    """,
}


async def purge_once() -> dict[str, int]:
    """한 번 돌린다. 지운 건수를 돌려준다."""
    지움 = {}
    async with AsyncSessionLocal() as db:
        for 이름, sql in SQL.items():
            p = {"mark": MARK} if ":mark" in sql else {}
            지움[이름] = (await db.execute(sq(sql), p)).rowcount or 0
        await db.commit()
    return 지움


async def run_forever() -> None:
    """서버가 사는 동안 하루에 한 번 돈다.

    한 번 실패해도 멈추지 않는다. DB 가 잠깐 끊겨서 한 번 걸렀다고 파기를
    영영 안 하게 되면, 정책이 조용히 깨진 채로 오래 간다.
    """
    await asyncio.sleep(FIRST_DELAY)
    while True:
        try:
            지움 = await purge_once()
            if any(지움.values()):
                log.info(
                    "보관기한 파기 — 원문 %d · 근거 %d · 메모 %d · 증거 %d",
                    지움["본문"], 지움["근거"], 지움["메모"], 지움["증거"],
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("보관기한 파기에 실패했다. 다음 주기에 다시 시도한다")
        await asyncio.sleep(INTERVAL)
