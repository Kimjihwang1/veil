"""자동 숨김(차단어·자동 숨김 분류)이 유튜브에 실제로 나가고 이력이 남는지.

2026-09-30 까지는 판별이 '숨김' 으로 정해도 우리 DB 상태만 바뀌고 유튜브 호출이
없었다. 관리 기준 화면의 토글이 거짓이 되는 구멍이라 여기서 고정한다.
"""

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.db.models import Action, Channel, Comment, RiskAssessment, Workspace
from app.db.session import AsyncSessionLocal
from app.services import auto_hide
from app.services import youtube_actions as yt
from app.services.pipeline import HIDDEN, QUEUE_JUDGE, PipelineResult

pytestmark = pytest.mark.asyncio

YT = "UC-autohide-test"


async def _purge():
    async with AsyncSessionLocal() as db:
        chs = list((await db.execute(
            select(Channel.id).where(Channel.youtube_channel_id == YT)
        )).scalars().all()) or [-1]
        cids = list((await db.execute(
            select(Comment.id).where(Comment.channel_id.in_(chs))
        )).scalars().all()) or [-1]
        await db.execute(delete(Action).where(Action.comment_id.in_(cids)))
        await db.execute(delete(RiskAssessment).where(RiskAssessment.comment_id.in_(cids)))
        await db.execute(delete(Comment).where(Comment.id.in_(cids)))
        wids = list((await db.execute(
            select(Channel.workspace_id).where(Channel.id.in_(chs))
        )).scalars().all())
        await db.execute(delete(Channel).where(Channel.id.in_(chs)))
        if wids:
            await db.execute(delete(Workspace).where(Workspace.id.in_(wids)))
        await db.commit()


@pytest_asyncio.fixture
async def 세상():
    await _purge()
    async with AsyncSessionLocal() as db:
        ws = Workspace(name="autohide ws", type="personal")
        db.add(ws)
        await db.flush()
        ch = Channel(
            workspace_id=ws.id, youtube_channel_id=YT, channel_title="자동숨김 시험",
            youtube_refresh_token="rt-test",
        )
        db.add(ch)
        await db.flush()
        cs = [
            Comment(channel_id=ch.id, youtube_comment_id="ah-1", content="차단어 걸림", status="hidden"),
            Comment(channel_id=ch.id, youtube_comment_id="ah-2", content="스팸 분류", status="hidden"),
            Comment(channel_id=ch.id, youtube_comment_id="ah-3", content="큐로 감", status="queued"),
        ]
        db.add_all(cs)
        await db.commit()
        ids = {"channel": ch.id, "c": [c.id for c in cs]}
    yield ids
    await _purge()


def _results():
    return [
        PipelineResult(text="차단어 걸림", destination=HIDDEN, decided_by="rule",
                       reason="관리자 차단어 '시발'", rule_value="시발", rule_action="block"),
        PipelineResult(text="스팸 분류", destination=HIDDEN, decided_by="llm",
                       reason="홍보 링크", llm_label="harmful", llm_category="스팸"),
        PipelineResult(text="큐로 감", destination=QUEUE_JUDGE, decided_by="llm",
                       reason="모욕", llm_label="harmful", llm_category="모욕"),
    ]


class TestAutoHide:
    async def test_숨김_행선지만_유튜브로_가고_이력이_남는다(self, 세상, monkeypatch):
        보낸 = []

        async def fake_set(token, ids, status):
            보낸.append((token, sorted(ids), status))
            return yt.ActionResult(True)

        monkeypatch.setattr(auto_hide.yt, "set_moderation", fake_set)
        async with AsyncSessionLocal() as db:
            ch = await db.get(Channel, 세상["channel"])
            st = await auto_hide.apply_auto_hides(db, ch, list(zip(세상["c"], _results())))

        assert st.attempted == 2 and st.synced == 2 and st.failed == 0
        assert 보낸 == [("rt-test", ["ah-1", "ah-2"], yt.HIDE)]   # 큐로 간 ah-3 은 안 보냄

        async with AsyncSessionLocal() as db:
            acts = (await db.execute(
                select(Action).where(Action.comment_id.in_(세상["c"])).order_by(Action.comment_id)
            )).scalars().all()
        assert [a.action_type for a in acts] == ["hide", "hide"]
        assert all(a.youtube_synced for a in acts)
        assert acts[0].actor == "자동(등록어: 시발)"
        assert acts[1].actor == "자동(분류: 스팸)"

    async def test_유튜브가_실패하면_미반영으로_남긴다(self, 세상, monkeypatch):
        async def fake_set(token, ids, status):
            return yt.ActionResult(False, "403 forbidden")

        monkeypatch.setattr(auto_hide.yt, "set_moderation", fake_set)
        async with AsyncSessionLocal() as db:
            ch = await db.get(Channel, 세상["channel"])
            st = await auto_hide.apply_auto_hides(db, ch, list(zip(세상["c"], _results())))
        assert st.synced == 0 and st.failed == 2
        async with AsyncSessionLocal() as db:
            acts = (await db.execute(select(Action).where(Action.comment_id.in_(세상["c"])))).scalars().all()
        assert len(acts) == 2 and not any(a.youtube_synced for a in acts)
        assert all("유튜브 반영 실패" in (a.note or "") for a in acts)

    async def test_토큰_없는_채널은_호출_없이_미연동_기록(self, 세상, monkeypatch):
        async def boom(*a, **k):
            raise AssertionError("토큰 없는 채널은 유튜브를 부르면 안 된다")

        monkeypatch.setattr(auto_hide.yt, "set_moderation", boom)
        async with AsyncSessionLocal() as db:
            ch = await db.get(Channel, 세상["channel"])
            ch.youtube_refresh_token = None
            await db.commit()
            st = await auto_hide.apply_auto_hides(db, ch, list(zip(세상["c"], _results())))
        assert st.not_connected == 2 and st.synced == 0
        async with AsyncSessionLocal() as db:
            acts = (await db.execute(select(Action).where(Action.comment_id.in_(세상["c"])))).scalars().all()
        assert len(acts) == 2 and all("미연동" in (a.note or "") for a in acts)

    async def test_관리자가_이미_처리한_댓글은_건드리지_않는다(self, 세상, monkeypatch):
        보낸 = []

        async def fake_set(token, ids, status):
            보낸.append(ids)
            return yt.ActionResult(True)

        monkeypatch.setattr(auto_hide.yt, "set_moderation", fake_set)
        from datetime import UTC, datetime
        async with AsyncSessionLocal() as db:
            c = await db.get(Comment, 세상["c"][0])
            c.reviewed_at = datetime.now(UTC).replace(tzinfo=None)
            await db.commit()
            ch = await db.get(Channel, 세상["channel"])
            st = await auto_hide.apply_auto_hides(db, ch, list(zip(세상["c"], _results())))
        assert st.skipped == 1 and st.attempted == 1
        assert 보낸 == [["ah-2"]]

    async def test_숨김이_하나도_없으면_아무것도_안_한다(self, 세상, monkeypatch):
        async def boom(*a, **k):
            raise AssertionError("부르면 안 된다")

        monkeypatch.setattr(auto_hide.yt, "set_moderation", boom)
        only_queue = [PipelineResult(text="x", destination=QUEUE_JUDGE, decided_by="llm", llm_label="ambiguous")]
        async with AsyncSessionLocal() as db:
            ch = await db.get(Channel, 세상["channel"])
            st = await auto_hide.apply_auto_hides(db, ch, [(세상["c"][2], only_queue[0])])
        assert st.attempted == 0

    async def test_유튜브가_보류한_댓글을_가리면_거부로_바뀐다(self, 세상, monkeypatch):
        # 보류 표시가 남으면 DB 에서 '숨김인데 유튜브는 보류' 로 읽힌다 (2026-11-04).
        async with AsyncSessionLocal() as db:
            c = await db.get(Comment, 세상["c"][1])
            c.youtube_moderation = "heldForReview"
            await db.commit()

        async def ok(*a, **k):
            return yt.ActionResult(True)

        monkeypatch.setattr(auto_hide.yt, "set_moderation", ok)
        async with AsyncSessionLocal() as db:
            ch = await db.get(Channel, 세상["channel"])
            await auto_hide.apply_auto_hides(db, ch, list(zip(세상["c"], _results())))

        async with AsyncSessionLocal() as db:
            assert (await db.get(Comment, 세상["c"][1])).youtube_moderation == yt.HIDE
            assert (await db.get(Comment, 세상["c"][0])).youtube_moderation is None   # 평범한 댓글은 그대로
