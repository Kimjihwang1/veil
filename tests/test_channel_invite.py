"""채널 연결 초대 링크.

관리자는 유튜버의 구글 계정을 모른다. 유튜버가 링크를 열어 권한만 주면
채널이 관리자 워크스페이스에 붙어야 한다. 지키는 것:

  1. 링크는 만든 사람 워크스페이스로만 향한다 (state 에 실림).
  2. 링크는 한 번만, 7일만.
  3. 유튜버 쪽 페이지·API 는 로그인 없이 되되, 주는 정보는 최소(초대자 이름, 워크스페이스 이름).
  4. 콜백은 초대 링크로 왔을 때 /app 이 아니라 /connect/done 으로 보낸다 —
     유튜버는 우리 사이트 로그인이 없다.
"""

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.core.deps import COOKIE
from app.db.models import ChannelInvite, Session, User, Workspace, WorkspaceMember
from app.db.session import AsyncSessionLocal
from app.main import app
from app.services import google_oauth as goog

pytestmark = pytest.mark.asyncio

메일 = "invite-admin@test.local"


async def _purge():
    async with AsyncSessionLocal() as db:
        u = (await db.execute(select(User).where(User.email == 메일))).scalar_one_or_none()
        if not u:
            return
        ws = list(
            (await db.execute(select(WorkspaceMember.workspace_id).where(WorkspaceMember.user_id == u.id))).scalars().all()
        ) or [-1]
        await db.execute(delete(ChannelInvite).where(ChannelInvite.workspace_id.in_(ws)))
        await db.execute(delete(Session).where(Session.user_id == u.id))
        await db.execute(delete(WorkspaceMember).where(WorkspaceMember.user_id == u.id))
        await db.execute(delete(User).where(User.id == u.id))
        await db.execute(delete(Workspace).where(Workspace.id.in_(ws)))
        await db.commit()


@pytest_asyncio.fixture
async def 관리자():
    await _purge()
    async with AsyncSessionLocal() as db:
        ws = Workspace(name="MCN 팀", type="team")
        db.add(ws)
        await db.flush()
        u = User(email=메일, name="민준")
        db.add(u)
        await db.flush()
        db.add(WorkspaceMember(workspace_id=ws.id, user_id=u.id, role="owner"))
        s = Session.new(u.id)
        db.add(s)
        await db.commit()
        ids = {"user": u.id, "ws": ws.id, "token": s.token}
    yield ids
    await _purge()


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _make(client, 관리자, note="진용진 채널"):
    r = await client.post(
        "/api/channels/invites", json={"note": note}, cookies={COOKIE: 관리자["token"]}
    )
    assert r.status_code == 201, r.text
    return r.json()


class TestCreate:
    async def test_만들면_링크와_기한이_온다(self, client, 관리자):
        inv = await _make(client, 관리자)
        assert "/connect/" in inv["url"] and inv["note"] == "진용진 채널"
        assert inv["used_at"] is None
        만료 = datetime.fromisoformat(inv["expires_at"])
        assert timedelta(days=6, hours=23) < 만료 - datetime.now(UTC).replace(tzinfo=None) <= timedelta(days=7)

    async def test_로그인_없이는_못_만든다(self, client):
        r = await client.post("/api/channels/invites", json={"note": "x"})
        assert r.status_code == 401

    async def test_목록은_내_워크스페이스_것만(self, client, 관리자):
        await _make(client, 관리자, "A")
        await _make(client, 관리자, "B")
        r = await client.get("/api/channels/invites", cookies={COOKIE: 관리자["token"]})
        assert [i["note"] for i in r.json()] == ["B", "A"]


class TestPublicSide:
    async def test_유튜버는_로그인_없이_초대자만_본다(self, client, 관리자):
        inv = await _make(client, 관리자)
        token = inv["url"].rsplit("/", 1)[1]
        r = await client.get(f"/api/channels/invites/{token}/info")
        assert r.status_code == 200
        body = r.json()
        assert body["valid"] and body["inviter_name"] == "민준" and body["workspace_name"] == "MCN 팀"
        assert "token" not in body and "workspace_id" not in body

    async def test_없는_링크(self, client):
        r = await client.get("/api/channels/invites/없는토큰/info")
        assert r.status_code == 200 and r.json()["valid"] is False

    async def test_쓴_링크와_지난_링크는_무효(self, client, 관리자):
        inv = await _make(client, 관리자)
        token = inv["url"].rsplit("/", 1)[1]
        async with AsyncSessionLocal() as db:
            row = (await db.execute(select(ChannelInvite).where(ChannelInvite.token == token))).scalar_one()
            row.used_at = datetime.now(UTC).replace(tzinfo=None)
            await db.commit()
        assert (await client.get(f"/api/channels/invites/{token}/info")).json()["reason"].startswith("이미")

        inv2 = await _make(client, 관리자)
        token2 = inv2["url"].rsplit("/", 1)[1]
        async with AsyncSessionLocal() as db:
            row = (await db.execute(select(ChannelInvite).where(ChannelInvite.token == token2))).scalar_one()
            row.expires_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=1)
            await db.commit()
        assert "기한" in (await client.get(f"/api/channels/invites/{token2}/info")).json()["reason"]

        # 무효 링크로 권한 주기를 누르면 결과 페이지로 보낸다 (구글로 안 간다)
        r = await client.get(f"/api/channels/connect/invite/{token}", follow_redirects=False)
        assert r.status_code in (302, 307) and "/connect/done?error=invite_invalid" in r.headers["location"]

    async def test_초대_페이지는_로그인_없이_열린다(self, client):
        r = await client.get("/connect/아무토큰")
        assert r.status_code == 200 and "채널 연결 요청" in r.text
        r = await client.get("/connect/done?connected=1")
        assert r.status_code == 200


class TestStateCarriesWorkspace:
    async def test_권한_주기는_관리자_워크스페이스를_state에_싣는다(self, client, 관리자, monkeypatch):
        """콜백은 state 의 workspace_id 로 채널을 붙인다. 링크가 그 값을 실어야
        유튜버가 자기 계정으로 눌러도 채널이 관리자 쪽으로 간다."""
        monkeypatch.setattr(get_settings_module(), "google_client_id", "cid")
        monkeypatch.setattr(get_settings_module(), "google_client_secret", "sec")
        inv = await _make(client, 관리자)
        token = inv["url"].rsplit("/", 1)[1]
        r = await client.get(f"/api/channels/connect/invite/{token}", follow_redirects=False)
        assert r.status_code in (302, 307)
        loc = r.headers["location"]
        assert loc.startswith(goog.AUTH_URL)
        from urllib.parse import parse_qs, urlparse

        state = parse_qs(urlparse(loc).query)["state"][0]
        data = goog.states.take(state)
        assert data["kind"] == "connect"
        assert data["workspace_id"] == 관리자["ws"] and data["user_id"] == 관리자["user"]
        assert data["invite_id"] == inv["id"]


class TestCallback:
    async def test_쓴_링크로_돌아온_콜백은_붙이지_않는다(self, client, 관리자, monkeypatch):
        # 같은 링크를 두 탭에서 열면 둘 다 구글 화면으로 넘어간다. 먼저 끝난 쪽이 링크를
        # 쓰고 나면, 늦게 돌아온 쪽은 붙이면 안 된다 (2026-11-09 전에는 붙였다).
        inv = await _make(client, 관리자)
        async with AsyncSessionLocal() as db:
            row = await db.get(ChannelInvite, inv["id"])
            row.used_at = datetime.now(UTC).replace(tzinfo=None)
            await db.commit()
        state = goog.states.issue(
            kind="connect", user_id=관리자["user"], workspace_id=관리자["ws"], invite_id=inv["id"]
        )
        불림 = []

        async def 가짜교환(*a, **k):
            불림.append(1)
            raise AssertionError("구글까지 가면 안 된다")

        monkeypatch.setattr(goog, "exchange_code", 가짜교환)
        r = await client.get(f"/api/channels/connect/callback?state={state}&code=x", follow_redirects=False)
        assert r.headers["location"] == "/connect/done?error=invite_invalid"
        assert 불림 == []

    async def test_구글_통신이_실패하면_안내_페이지로(self, client, 관리자, monkeypatch):
        # 전에는 처리 안 된 500 이 유튜버에게 그대로 보였다.
        inv = await _make(client, 관리자)
        state = goog.states.issue(
            kind="connect", user_id=관리자["user"], workspace_id=관리자["ws"], invite_id=inv["id"]
        )

        async def 터짐(*a, **k):
            raise RuntimeError("구글 장애")

        monkeypatch.setattr(goog, "exchange_code", 터짐)
        r = await client.get(f"/api/channels/connect/callback?state={state}&code=x", follow_redirects=False)
        assert r.headers["location"] == "/connect/done?error=google_failed"


def get_settings_module():
    from app.core.config import get_settings

    return get_settings()
