"""조치가 유튜브로 나가는 경로.

되돌리기 어려운 동작이라 고정해둔다. 특히 두 가지.

  1. 유튜브 호출이 실패해도 관리자 판단은 DB 에 남아야 한다.
     실패했다고 없던 일로 만들면, 관리자는 같은 댓글을 또 보게 된다.
  2. youtube_synced 는 '실제로 반영됐는가' 여야 한다.
     호출할 게 없었던 경우까지 True 로 두면 이력에서 구분이 안 된다.
"""

from datetime import datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.core.deps import COOKIE
from app.db.models import (
    Action,
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
from app.services import youtube_actions as yt

pytestmark = pytest.mark.asyncio


async def _ok(*a, **k):
    return yt.ActionResult(True)


async def _true(*a, **k):
    return True


async def _false(*a, **k):
    return False

메일 = "yt@test.local"


async def _purge():
    async with AsyncSessionLocal() as db:
        u = (
            await db.execute(select(User).where(User.email == 메일))
        ).scalar_one_or_none()
        if not u:
            return
        ws = list(
            (
                await db.execute(
                    select(WorkspaceMember.workspace_id).where(
                        WorkspaceMember.user_id == u.id
                    )
                )
            ).scalars().all()
        ) or [-1]
        chans = list(
            (
                await db.execute(select(Channel.id).where(Channel.workspace_id.in_(ws)))
            ).scalars().all()
        ) or [-1]
        cmts = list(
            (
                await db.execute(select(Comment.id).where(Comment.channel_id.in_(chans)))
            ).scalars().all()
        ) or [-1]
        await db.execute(delete(Action).where(Action.comment_id.in_(cmts)))
        await db.execute(
            delete(RiskAssessment).where(RiskAssessment.comment_id.in_(cmts))
        )
        await db.execute(delete(Comment).where(Comment.id.in_(cmts)))
        await db.execute(delete(Channel).where(Channel.id.in_(chans)))
        await db.execute(delete(Session).where(Session.user_id == u.id))
        await db.execute(delete(WorkspaceMember).where(WorkspaceMember.user_id == u.id))
        await db.execute(delete(User).where(User.id == u.id))
        await db.execute(delete(Workspace).where(Workspace.id.in_(ws)))
        await db.commit()


@pytest_asyncio.fixture
async def 세상():
    """연동된 채널 하나와, 큐에 있는 댓글 하나."""
    await _purge()
    async with AsyncSessionLocal() as db:
        ws = Workspace(name="yt ws", type="personal")
        db.add(ws)
        await db.flush()
        u = User(email=메일, name="yt")
        db.add(u)
        await db.flush()
        db.add(WorkspaceMember(workspace_id=ws.id, user_id=u.id, role="owner"))

        ch = Channel(
            workspace_id=ws.id,
            youtube_channel_id="UC-yt",
            channel_title="연동된 채널",
            youtube_refresh_token="가짜-리프레시-토큰",
        )
        db.add(ch)
        await db.flush()

        c = Comment(
            channel_id=ch.id,
            youtube_comment_id="yt-cmt-1",
            content="숨길 댓글",
            status="queued",
        )
        db.add(c)
        await db.flush()
        db.add(
            RiskAssessment(
                comment_id=c.id, stage="llm", destination="queue_judge",
                risk_level="harmful", category="모욕",
            )
        )
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


async def _action(client, 세상, **body):
    return await client.post(
        f"/api/comments/{세상['comment']}/action",
        json=body,
        cookies={COOKIE: 세상["token"]},
    )


async def _actions(comment_id):
    async with AsyncSessionLocal() as db:
        return (
            await db.execute(select(Action).where(Action.comment_id == comment_id))
        ).scalars().all()


class TestHide:
    async def test_숨김이_유튜브로_나간다(self, client, 세상, monkeypatch):
        불린것 = {}

        async def 가짜(refresh, ids, status):
            불린것.update(refresh=refresh, ids=ids, status=status)
            return yt.ActionResult(True)

        monkeypatch.setattr(yt, "set_moderation", 가짜)
        r = await _action(client, 세상, action="hide")

        assert r.status_code == 200
        assert r.json()["youtube_synced"] is True
        assert 불린것["ids"] == ["yt-cmt-1"]
        assert 불린것["status"] == yt.HIDE      # 'rejected' — 삭제가 아니라 비공개
        assert 불린것["refresh"] == "가짜-리프레시-토큰"

    async def test_유튜브가_실패해도_판단은_남는다(self, client, 세상, monkeypatch):
        async def 실패(*a, **k):
            return yt.ActionResult(False, "권한이 모자랍니다")

        monkeypatch.setattr(yt, "set_moderation", 실패)
        r = await _action(client, 세상, action="hide", note="관리자 메모")

        assert r.status_code == 200
        assert r.json()["youtube_synced"] is False
        assert "권한이 모자랍니다" in r.json()["note"]
        assert "관리자 메모" in r.json()["note"]   # 원래 메모도 지우지 않는다

        기록 = await _actions(세상["comment"])
        assert len(기록) == 1, "유튜브가 실패해도 이력은 남아야 한다"
        assert 기록[0].youtube_synced is False

    async def test_유튜브가_터져도_판단은_남는다(self, client, 세상, monkeypatch):
        async def 터짐(*a, **k):
            raise RuntimeError("네트워크 끊김")

        monkeypatch.setattr(yt, "set_moderation", 터짐)
        r = await _action(client, 세상, action="hide")

        assert r.status_code == 200
        assert r.json()["youtube_synced"] is False
        assert len(await _actions(세상["comment"])) == 1


class TestKeep:
    async def test_공개중인_것을_유지하면_유튜브를_안_부른다(
        self, client, 세상, monkeypatch
    ):
        """반영할 게 없다. 그렇다고 synced=True 로 두면 이력이 거짓이 된다."""
        불렸나 = {"yes": False}

        async def 가짜(*a, **k):
            불렸나["yes"] = True
            return yt.ActionResult(True)

        monkeypatch.setattr(yt, "set_moderation", 가짜)
        r = await _action(client, 세상, action="keep")

        assert r.status_code == 200
        assert 불렸나["yes"] is False, "공개 상태면 유튜브를 부를 이유가 없다"
        assert r.json()["youtube_synced"] is False

    async def test_숨겼던_것을_유지하면_공개로_되돌린다(
        self, client, 세상, monkeypatch
    ):
        async with AsyncSessionLocal() as db:
            c = await db.get(Comment, 세상["comment"])
            c.status = "hidden"
            await db.commit()

        불린것 = {}

        async def 가짜(refresh, ids, status):
            불린것["status"] = status
            return yt.ActionResult(True)

        monkeypatch.setattr(yt, "set_moderation", 가짜)
        monkeypatch.setattr(yt, "is_published", lambda *a, **k: _true())
        r = await _action(client, 세상, action="keep")

        assert 불린것["status"] == yt.PUBLISH
        assert r.json()["youtube_synced"] is True

    async def test_복구는_유튜브가_안_풀어주면_미반영으로_남긴다(
        self, client, 세상, monkeypatch
    ):
        """published 호출이 204 여도 댓글은 그대로 가려져 있을 수 있다 — 실제로
        그렇다. 호출 성공을 반영으로 치면 관리자는 공개된 줄 안다."""
        async with AsyncSessionLocal() as db:
            c = await db.get(Comment, 세상["comment"])
            c.status = "hidden"
            await db.commit()

        monkeypatch.setattr(yt, "set_moderation", lambda *a, **k: _ok())
        monkeypatch.setattr(yt, "is_published", lambda *a, **k: _false())
        r = await _action(client, 세상, action="keep")

        body = r.json()
        assert body["youtube_synced"] is False
        assert "되돌리지 못" in (body["note"] or "")
        assert body["status"] == "queued"          # 우리 기록은 큐로 돌아간다

    async def test_복구하면_검토_큐로_돌아간다(self, client, 세상, monkeypatch):
        """숨긴 걸 되돌리면 '아직 판단 안 함' 으로 가야 한다.

        통과로 처리하면 그 댓글이 숨김 목록에서도 검토 큐에서도 빠져서,
        잘못 숨겼다가 되돌린 댓글을 다시 볼 방법이 없어진다.
        """
        async with AsyncSessionLocal() as db:
            c = await db.get(Comment, 세상["comment"])
            c.status = "hidden"
            c.reviewed_at = datetime(2020, 1, 1)
            await db.commit()

        monkeypatch.setattr(yt, "set_moderation", lambda *a, **k: _ok())
        monkeypatch.setattr(yt, "is_published", lambda *a, **k: _true())
        r = await _action(client, 세상, action="keep")
        assert r.json()["status"] == "queued"

        async with AsyncSessionLocal() as db:
            c = await db.get(Comment, 세상["comment"])
            # 검토 큐는 status 와 reviewed_at 을 함께 본다. 하나만 되돌리면
            # 어느 목록에도 안 뜬다 — 실제로 그렇게 사라진 적이 있다.
            assert c.status == "queued"
            assert c.reviewed_at is None

    async def test_검토_큐에서_유지하면_통과로_남는다(
        self, client, 세상, monkeypatch
    ):
        """같은 keep 이라도 공개중인 걸 유지한 것은 판단이 끝난 것이다."""
        r = await _action(client, 세상, action="keep")
        assert r.json()["status"] == "passed"

        async with AsyncSessionLocal() as db:
            c = await db.get(Comment, 세상["comment"])
            assert c.status == "passed"
            assert c.reviewed_at is not None


class TestDuplicate:
    async def test_hiding_hidden_comment_is_409(self, client, 세상, monkeypatch):
        """이미 유튜브에서 가려진 걸 또 가리면 이력만 쌓인다 → 409."""
        monkeypatch.setattr(yt, "set_moderation", lambda *a, **k: _ok())
        first = await _action(client, 세상, action="hide")
        assert first.status_code == 200

        second = await _action(client, 세상, action="hide")
        assert second.status_code == 409
        assert len(await _actions(세상["comment"])) == 1

    async def test_유튜브_반영에_실패한_숨김은_다시_보낼_수_있다(self, client, 세상, monkeypatch):
        """토큰 만료 등으로 유튜브 반영이 실패한 숨김은 '다시 보내기' 가 유일한 재시도 길이다.
        큐로 되돌리기 버튼을 뺐으니(2026-09-30) 이게 막히면 거짓 상태가 영원히 남는다."""
        async def bad(*a, **k):
            return yt.ActionResult(False, "403 forbidden")

        monkeypatch.setattr(yt, "set_moderation", bad)
        first = await _action(client, 세상, action="hide")
        assert first.status_code == 200 and first.json()["youtube_synced"] is False

        monkeypatch.setattr(yt, "set_moderation", lambda *a, **k: _ok())
        second = await _action(client, 세상, action="hide")
        assert second.status_code == 200, second.text
        assert second.json()["youtube_synced"] is True
        acts = await _actions(세상["comment"])
        assert len(acts) == 2 and acts[-1].youtube_synced

        third = await _action(client, 세상, action="hide")
        assert third.status_code == 409


class TestNotConnected:
    async def test_미연동_채널은_안내가_남는다(self, client, 세상, monkeypatch):
        async with AsyncSessionLocal() as db:
            ch = await db.get(Channel, 세상["channel"])
            ch.youtube_refresh_token = None
            await db.commit()

        r = await _action(client, 세상, action="hide")

        assert r.status_code == 200
        assert r.json()["youtube_synced"] is False
        assert "연동" in r.json()["note"]
        # 유튜브에 못 보냈어도 우리 DB 에서는 숨김 처리가 된다
        assert r.json()["status"] == "hidden"


class TestExpiredToken:
    """구글 앱이 테스트 상태면 리프레시 토큰이 7일 뒤 만료된다. 그때 '유튜브 오류: RuntimeError'
    가 아니라 '권한 만료 → 다시 연결' 로 읽혀야 관리자가 뭘 할지 안다 (2026-09-30 실제로 겪음)."""

    async def test_invalid_grant_는_다시_연결_안내로_바뀐다(self, monkeypatch):
        from types import SimpleNamespace

        from app.services import youtube_actions as yt

        monkeypatch.setattr(
            yt, "get_settings",
            lambda: SimpleNamespace(oauth_ready=True, google_client_id="id", google_client_secret="s"),
        )

        async def dead(*a, **k):
            raise RuntimeError("[Google OAuth] invalid_grant: Token has been expired or revoked.")

        monkeypatch.setattr(yt, "refresh_access_token", dead)
        with pytest.raises(yt.NotConnected) as ex:
            await yt._access_token("rt-old")
        assert "유튜브 권한이 만료" in str(ex.value) and "다시 연결" in str(ex.value)

    async def test_다른_오류는_그대로_올라간다(self, monkeypatch):
        from types import SimpleNamespace

        from app.services import youtube_actions as yt

        monkeypatch.setattr(
            yt, "get_settings",
            lambda: SimpleNamespace(oauth_ready=True, google_client_id="id", google_client_secret="s"),
        )

        async def boom(*a, **k):
            raise RuntimeError("[Google OAuth] server_error")

        monkeypatch.setattr(yt, "refresh_access_token", boom)
        with pytest.raises(RuntimeError):
            await yt._access_token("rt")


class TestHeld:
    """유튜브가 보류·스팸 의심으로 잡아 둔 댓글 (2026-11-04).

    유튜브에서 지금 안 보이는 댓글이라 [유지] 는 '공개해도 된다' 는 뜻이다.
    우리 기록만 통과로 바꾸면 유튜브에서는 영영 보류로 남는다.
    """

    async def _보류로(self, 세상, 상태="heldForReview"):
        async with AsyncSessionLocal() as db:
            c = await db.get(Comment, 세상["comment"])
            c.youtube_moderation = 상태
            await db.commit()

    async def test_큐에서_보류인지_알려준다(self, client, 세상):
        await self._보류로(세상, "likelySpam")
        r = await client.get(
            f"/api/channels/{세상['channel']}/queue", cookies={COOKIE: 세상["token"]}
        )
        assert r.json()[0]["youtube_held"] == "likelySpam"

    async def test_보류_댓글을_유지하면_유튜브에_공개를_보낸다(self, client, 세상, monkeypatch):
        await self._보류로(세상)
        불린것 = {}

        async def 가짜(refresh, ids, status):
            불린것["status"] = status
            return yt.ActionResult(True)

        monkeypatch.setattr(yt, "set_moderation", 가짜)
        r = await _action(client, 세상, action="keep")

        assert 불린것["status"] == yt.PUBLISH
        assert r.json()["youtube_synced"] is True
        assert r.json()["status"] == "passed"
        async with AsyncSessionLocal() as db:
            assert (await db.get(Comment, 세상["comment"])).youtube_moderation == yt.PUBLISH

    async def test_보류_댓글을_숨기면_거부로_바뀐다(self, client, 세상, monkeypatch):
        await self._보류로(세상)
        monkeypatch.setattr(yt, "set_moderation", lambda *a, **k: _ok())
        r = await _action(client, 세상, action="hide")

        assert r.json()["youtube_synced"] is True
        async with AsyncSessionLocal() as db:
            assert (await db.get(Comment, 세상["comment"])).youtube_moderation == yt.HIDE

    async def test_공개가_실패하면_보류_표시를_남긴다(self, client, 세상, monkeypatch):
        await self._보류로(세상)

        async def 실패(*a, **k):
            return yt.ActionResult(False, "권한이 모자랍니다")

        monkeypatch.setattr(yt, "set_moderation", 실패)
        r = await _action(client, 세상, action="keep")

        assert r.json()["youtube_synced"] is False
        assert "권한이 모자랍니다" in r.json()["note"]
        async with AsyncSessionLocal() as db:
            assert (await db.get(Comment, 세상["comment"])).youtube_moderation == "heldForReview"
