"""AI 판별 동의 (YouTube API 정책).

동의 칸을 만들어두고 확인하지 않으면 정책이 문서에만 있는 셈이 된다.
실제로 판별이 막히는지, 철회했을 때 기존 판정이 남는지를 고정한다.
"""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.core.deps import COOKIE
from app.db.models import (
    Channel,
    Comment,
    RiskAssessment,
    Session,
    User,
    Workspace,
    WorkspaceMember,
)
from app.db.session import AsyncSessionLocal
from app.main import app

pytestmark = pytest.mark.asyncio

메일 = "consent@test.local"
YT = "UC-consent-test"


async def _purge():
    async with AsyncSessionLocal() as db:
        u = (
            await db.execute(select(User).where(User.email == 메일))
        ).scalar_one_or_none()
        if not u:
            return
        wids = list(
            (
                await db.execute(
                    select(WorkspaceMember.workspace_id).where(
                        WorkspaceMember.user_id == u.id
                    )
                )
            ).scalars().all()
        ) or [-1]
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
        await db.execute(delete(RiskAssessment).where(RiskAssessment.comment_id.in_(cids)))
        await db.execute(delete(Comment).where(Comment.id.in_(cids)))
        await db.execute(delete(Channel).where(Channel.id.in_(chs)))
        await db.execute(delete(Session).where(Session.user_id == u.id))
        await db.execute(delete(WorkspaceMember).where(WorkspaceMember.user_id == u.id))
        await db.execute(delete(User).where(User.id == u.id))
        await db.execute(delete(Workspace).where(Workspace.id.in_(wids)))
        await db.commit()


@pytest_asyncio.fixture
async def 세상():
    await _purge()
    async with AsyncSessionLocal() as db:
        ws = Workspace(name="동의 ws", type="personal")
        db.add(ws)
        await db.flush()
        u = User(email=메일, name="c")
        db.add(u)
        await db.flush()
        db.add(WorkspaceMember(workspace_id=ws.id, user_id=u.id, role="owner"))

        ch = Channel(
            workspace_id=ws.id, youtube_channel_id=YT, channel_title="동의 시험"
        )
        db.add(ch)
        await db.flush()
        c = Comment(
            channel_id=ch.id, youtube_comment_id="consent-1",
            content="댓글", status="queued",
        )
        db.add(c)
        await db.flush()
        db.add(RiskAssessment(
            comment_id=c.id, stage="llm", risk_level="harmful", category="모욕"
        ))
        s = Session.new(u.id)
        db.add(s)
        await db.commit()
        ids = {"channel": ch.id, "comment": c.id, "token": s.token}

    yield ids
    await _purge()


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


class TestConsent:
    async def test_기본은_동의_안함(self, client, 세상):
        """새 채널은 동의 전이다. 켜져 있으면 안 된다."""
        r = await client.get(
            f"/api/channels/{세상['channel']}/consent",
            cookies={COOKIE: 세상["token"]},
        )
        assert r.status_code == 200
        assert r.json()["agreed"] is False
        assert r.json()["can_judge"] is False

    async def test_동의하면_시각이_남는다(self, client, 세상):
        r = await client.put(
            f"/api/channels/{세상['channel']}/consent",
            json={"agreed": True},
            cookies={COOKIE: 세상["token"]},
        )
        assert r.json()["agreed"] is True
        assert r.json()["agreed_at"] is not None
        assert r.json()["can_judge"] is True

    async def test_철회하면_시각도_지운다(self, client, 세상):
        await client.put(
            f"/api/channels/{세상['channel']}/consent",
            json={"agreed": True},
            cookies={COOKIE: 세상["token"]},
        )
        r = await client.put(
            f"/api/channels/{세상['channel']}/consent",
            json={"agreed": False},
            cookies={COOKIE: 세상["token"]},
        )
        assert r.json()["agreed"] is False
        assert r.json()["agreed_at"] is None

    async def test_철회해도_기존_판정은_남는다(self, client, 세상):
        """관리자가 실제로 보고 조치한 근거다. 소급해 없애면 이력이 깨진다."""
        await client.put(
            f"/api/channels/{세상['channel']}/consent",
            json={"agreed": True},
            cookies={COOKIE: 세상["token"]},
        )
        await client.put(
            f"/api/channels/{세상['channel']}/consent",
            json={"agreed": False},
            cookies={COOKIE: 세상["token"]},
        )
        async with AsyncSessionLocal() as db:
            ra = (
                await db.execute(
                    select(RiskAssessment).where(
                        RiskAssessment.comment_id == 세상["comment"]
                    )
                )
            ).scalars().all()
        assert len(ra) == 1
        assert ra[0].category == "모욕"

    async def test_남의_채널_동의는_못_바꾼다(self, client, 세상):
        r = await client.put(
            f"/api/channels/{세상['channel']}/consent",
            json={"agreed": True},
        )
        assert r.status_code == 401
