"""보관기한 파기.

YouTube API 정책상 댓글 원문·작성자는 최대 30일이다. 그 뒤에는 위험도 같은
파생 지표만 남겨야 한다. 지우는 범위를 잘못 잡으면 둘 중 하나가 된다 —
개인정보가 남거나, 통계가 과거로 소급해 바뀌거나.
"""

import subprocess
import sys
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy import text as sq

from app.db.models import Action, Channel, Comment, RiskAssessment, Workspace
from app.db.session import AsyncSessionLocal

pytestmark = pytest.mark.asyncio

YT = "UC-purge-test"
비움 = "(보관기한 경과로 삭제됨)"


async def _purge_fixture():
    async with AsyncSessionLocal() as db:
        chs = list(
            (
                await db.execute(
                    select(Channel.id).where(Channel.youtube_channel_id == YT)
                )
            ).scalars().all()
        ) or [-1]
        cids = list(
            (
                await db.execute(select(Comment.id).where(Comment.channel_id.in_(chs)))
            ).scalars().all()
        ) or [-1]
        await db.execute(delete(Action).where(Action.comment_id.in_(cids)))
        await db.execute(delete(RiskAssessment).where(RiskAssessment.comment_id.in_(cids)))
        await db.execute(delete(Comment).where(Comment.id.in_(cids)))
        await db.execute(delete(Channel).where(Channel.id.in_(chs)))
        await db.commit()


@pytest_asyncio.fixture
async def 세상():
    """기한이 지난 댓글 하나, 아직 남은 댓글 하나."""
    await _purge_fixture()
    async with AsyncSessionLocal() as db:
        ws = (await db.execute(select(Workspace).limit(1))).scalar_one_or_none()
        ch = Channel(
            workspace_id=ws.id if ws else None,
            youtube_channel_id=YT,
            channel_title="파기 시험",
        )
        db.add(ch)
        await db.flush()

        어제 = datetime.utcnow() - timedelta(days=1)
        내일 = datetime.utcnow() + timedelta(days=1)
        만든것 = {}
        for 이름, 기한 in (("지난것", 어제), ("남은것", 내일)):
            c = Comment(
                channel_id=ch.id,
                youtube_comment_id=f"purge-{이름}",
                content=f"{이름} 의 원문입니다",
                author_name="작성자이름",
                author_channel_id="UC-author",
                status="queued",
                retention_expires_at=기한,
            )
            db.add(c)
            await db.flush()
            db.add(RiskAssessment(
                comment_id=c.id, stage="llm", risk_level="harmful",
                category="모욕", reasoning="특정인을 깎아내린다",
            ))
            db.add(Action(comment_id=c.id, action_type="hide",
                          actor="a@b.c", note="관리자 메모"))
            만든것[이름] = c.id
        await db.commit()
        만든것["channel"] = ch.id

    yield 만든것
    await _purge_fixture()


def _돌리기(*args):
    return subprocess.run(
        [sys.executable, "-m", "scripts.purge_expired", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


class TestPurge:
    async def test_기한이_지난_것만_지운다(self, 세상):
        _돌리기()

        async with AsyncSessionLocal() as db:
            지난것 = await db.get(Comment, 세상["지난것"])
            남은것 = await db.get(Comment, 세상["남은것"])

        assert 지난것.content == 비움
        assert 지난것.author_name is None
        assert 지난것.author_channel_id is None

        assert 남은것.content == "남은것 의 원문입니다", "기한 전이면 건드리면 안 된다"
        assert 남은것.author_name == "작성자이름"

    async def test_판정_근거도_지운다(self, 세상):
        """근거 문장이 원문을 되짚어준다. 남기면 지운 의미가 없다."""
        _돌리기()

        async with AsyncSessionLocal() as db:
            지난 = (
                await db.execute(
                    select(RiskAssessment).where(
                        RiskAssessment.comment_id == 세상["지난것"]
                    )
                )
            ).scalar_one()
            남은 = (
                await db.execute(
                    select(RiskAssessment).where(
                        RiskAssessment.comment_id == 세상["남은것"]
                    )
                )
            ).scalar_one()

        assert 지난.reasoning is None
        assert 남은.reasoning == "특정인을 깎아내린다"

    async def test_판정_결과는_남긴다(self, 세상):
        """위험도·카테고리까지 지우면 통계가 과거로 소급해 바뀐다."""
        _돌리기()

        async with AsyncSessionLocal() as db:
            ra = (
                await db.execute(
                    select(RiskAssessment).where(
                        RiskAssessment.comment_id == 세상["지난것"]
                    )
                )
            ).scalar_one()
            c = await db.get(Comment, 세상["지난것"])

        assert ra.risk_level == "harmful"
        assert ra.category == "모욕"
        assert c.status == "queued", "행 자체는 남아야 집계가 흔들리지 않는다"

    async def test_조치_메모도_지운다(self, 세상):
        _돌리기()

        async with AsyncSessionLocal() as db:
            a = (
                await db.execute(
                    select(Action).where(Action.comment_id == 세상["지난것"])
                )
            ).scalar_one()

        assert a.note is None
        assert a.action_type == "hide", "무엇을 했는지는 남아야 한다"
        assert a.actor == "a@b.c", "누가 했는지도 (내부 계정이라 개인정보 아님)"

    async def test_dry_는_지우지_않는다(self, 세상):
        r = _돌리기("--dry")
        assert "실제로 지우려면" in r.stdout

        async with AsyncSessionLocal() as db:
            c = await db.get(Comment, 세상["지난것"])
        assert c.content == "지난것 의 원문입니다"

    async def test_두_번_돌려도_안전하다(self, 세상):
        _돌리기()
        r = _돌리기()
        assert "지울 것이 없다" in r.stdout or "0건" in r.stdout
