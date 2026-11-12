"""사람 라벨링 API.

지키려는 것 하나가 제일 크다 — **AI 판정이 화면으로 새면 안 된다.** 사람이
AI 답을 보면 따라가고, 그 답지로 AI 를 채점하면 점수가 부풀려진다.
"""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.db.models import Channel, Comment, LabelTask, RiskAssessment, Workspace
from app.db.session import AsyncSessionLocal
from app.main import app

pytestmark = pytest.mark.asyncio

이름 = "라벨테스트"
접두 = "UC-label-test"


async def _purge():
    async with AsyncSessionLocal() as db:
        chans = list((await db.execute(
            select(Channel.id).where(Channel.youtube_channel_id == 접두))).scalars().all()) or [-1]
        cmts = list((await db.execute(
            select(Comment.id).where(Comment.channel_id.in_(chans)))).scalars().all()) or [-1]
        wss = list((await db.execute(
            select(Workspace.id).where(Workspace.name == "label ws"))).scalars().all()) or [-1]
        await db.execute(delete(LabelTask).where(LabelTask.labeler == 이름))
        await db.execute(delete(RiskAssessment).where(RiskAssessment.comment_id.in_(cmts)))
        await db.execute(delete(Comment).where(Comment.id.in_(cmts)))
        await db.execute(delete(Channel).where(Channel.id.in_(chans)))
        await db.execute(delete(Workspace).where(Workspace.id.in_(wss)))
        await db.commit()


@pytest_asyncio.fixture
async def 세상():
    """댓글 3개, 그중 하나는 답글. 전부 AI 판정이 붙어 있다 — 그게 새는지 본다."""
    await _purge()
    async with AsyncSessionLocal() as db:
        ws = Workspace(name="label ws", type="personal"); db.add(ws); await db.flush()
        ch = Channel(workspace_id=ws.id, youtube_channel_id=접두, channel_title="라벨 채널")
        db.add(ch); await db.flush()
        부모 = Comment(channel_id=ch.id, youtube_comment_id="lt-parent",
                     content="부모 댓글입니다", status="passed")
        db.add(부모); await db.flush()
        cs = [
            Comment(channel_id=ch.id, youtube_comment_id="lt-1", content="첫 댓글", status="queued"),
            Comment(channel_id=ch.id, youtube_comment_id="lt-2", content="둘째 답글",
                    parent_comment_id="lt-parent", is_reply=True, status="queued"),
            Comment(channel_id=ch.id, youtube_comment_id="lt-3", content="셋째 댓글", status="passed"),
        ]
        db.add_all(cs); await db.flush()
        for c in cs:
            db.add(RiskAssessment(comment_id=c.id, stage="llm", destination="queue_judge",
                                  risk_level="harmful", category="모욕", reasoning="AI 근거 — 새면 안 됨"))
        for i, c in enumerate(cs, 1):
            db.add(LabelTask(labeler=이름, comment_id=c.id, position=i,
                             segment="공통" if i == 1 else "개별", source="큐"))
        await db.commit()
        ids = [c.id for c in cs]
    yield ids
    await _purge()


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


class TestNoLeak:
    async def test_응답_어디에도_AI_판정이_없다(self, client, 세상):
        r = await client.get(f"/api/label/next?who={이름}")
        assert r.status_code == 200
        본문 = r.text
        for 금지 in ("harmful", "ambiguous", "모욕", "risk_level", "category", "reason", "AI 근거"):
            assert 금지 not in 본문, f"AI 판정이 샜다: {금지}"

    async def test_보여주는_건_관리자가_보는_것과_같다(self, client, 세상):
        # 첫 번째는 부모 없는 댓글
        r = (await client.get(f"/api/label/next?who={이름}")).json()
        assert r["comment"]["content"] == "첫 댓글"
        assert r["comment"]["channel_title"] == "라벨 채널"
        assert r["comment"]["parent_content"] is None


class TestFlow:
    async def test_순서대로_주고_답하면_다음으로_넘어간다(self, client, 세상):
        r1 = (await client.get(f"/api/label/next?who={이름}")).json()
        assert r1["position"] == 1 and r1["done"] == 0 and r1["total"] == 3

        p = await client.post(f"/api/label/{r1['task_id']}", json={"label": "keep", "seconds": 2.5})
        assert p.status_code == 200 and p.json()["done"] == 1

        r2 = (await client.get(f"/api/label/next?who={이름}")).json()
        assert r2["position"] == 2
        assert r2["comment"]["parent_content"] == "부모 댓글입니다"   # 답글은 부모를 같이
        assert r2["last_task_id"] == r1["task_id"]

    async def test_취소하면_그_항목이_다시_온다(self, client, 세상):
        r1 = (await client.get(f"/api/label/next?who={이름}")).json()
        await client.post(f"/api/label/{r1['task_id']}", json={"label": "hide"})
        u = await client.post(f"/api/label/{r1['task_id']}/undo")
        assert u.json()["done"] == 0
        again = (await client.get(f"/api/label/next?who={이름}")).json()
        assert again["task_id"] == r1["task_id"]

    async def test_다_하면_task_id_가_없다(self, client, 세상):
        for _ in range(3):
            r = (await client.get(f"/api/label/next?who={이름}")).json()
            await client.post(f"/api/label/{r['task_id']}", json={"label": "unsure"})
        r = (await client.get(f"/api/label/next?who={이름}")).json()
        assert r["task_id"] is None and r["done"] == 3

    async def test_이상한_라벨은_거절(self, client, 세상):
        r1 = (await client.get(f"/api/label/next?who={이름}")).json()
        bad = await client.post(f"/api/label/{r1['task_id']}", json={"label": "maybe"})
        assert bad.status_code == 422

    async def test_배정_없는_사람은_404(self, client, 세상):
        assert (await client.get("/api/label/next?who=없는사람")).status_code == 404

    async def test_라벨러_목록에_진행률(self, client, 세상):
        rows = (await client.get("/api/label/labelers")).json()
        me = next(r for r in rows if r["name"] == 이름)
        assert me["total"] == 3 and me["done"] == 0
