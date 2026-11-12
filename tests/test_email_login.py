"""이메일 + 비밀번호 로그인.

지키는 것:
  1. 비밀번호 원문은 어디에도 남지 않는다 (해시만).
  2. 가입하면 바로 로그인 상태 + 개인 워크스페이스.
  3. 틀렸을 때 "이메일 없음" 과 "비밀번호 틀림" 을 가르지 않는다.
  4. Google 계정 이메일로는 비밀번호 가입이 안 된다 (계정 탈취 경로).
"""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.core.deps import COOKIE
from app.db.models import Session, User, Workspace, WorkspaceMember
from app.db.session import AsyncSessionLocal
from app.main import app
from app.services.password import hash_password, verify_password

pytestmark = pytest.mark.asyncio

메일 = "pw-test@test.local"
구글메일 = "google-only@test.local"


async def _purge():
    async with AsyncSessionLocal() as db:
        users = (
            await db.execute(select(User).where(User.email.in_([메일, 구글메일])))
        ).scalars().all()
        for u in users:
            ws = list(
                (
                    await db.execute(
                        select(WorkspaceMember.workspace_id).where(WorkspaceMember.user_id == u.id)
                    )
                ).scalars().all()
            )
            await db.execute(delete(Session).where(Session.user_id == u.id))
            await db.execute(delete(WorkspaceMember).where(WorkspaceMember.user_id == u.id))
            await db.execute(delete(User).where(User.id == u.id))
            if ws:
                await db.execute(delete(Workspace).where(Workspace.id.in_(ws)))
        await db.commit()


@pytest_asyncio.fixture(autouse=True)
async def 깨끗하게():
    await _purge()
    yield
    await _purge()


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


class TestPasswordHash:
    def test_원문이_저장값에_없다(self):
        h = hash_password("correct horse battery")
        assert "correct" not in h and h.startswith("scrypt$")

    def test_같은_비밀번호도_저장값은_다르다(self):
        assert hash_password("abcdefgh") != hash_password("abcdefgh")  # 소금이 다르다

    def test_검증(self):
        h = hash_password("abcdefgh")
        assert verify_password("abcdefgh", h)
        assert not verify_password("abcdefgx", h)
        assert not verify_password("abcdefgh", None)
        assert not verify_password("abcdefgh", "깨진값")

    def test_짧으면_거부(self):
        with pytest.raises(ValueError):
            hash_password("short")


class TestSignup:
    async def test_가입은_로그인시키지_않고_워크스페이스만_만든다(self, client):
        r = await client.post(
            "/api/auth/signup", json={"email": 메일, "password": "abcdefgh", "name": "지황"}
        )
        assert r.status_code == 201, r.text
        # 가입 = 계정 생성. 세션 쿠키는 안 준다. 로그인 화면에서 직접 쳐야 한다 (2026-09-30).
        assert COOKIE not in r.cookies

        r = await client.post("/api/auth/login", json={"email": 메일, "password": "abcdefgh"})
        assert r.status_code == 200, r.text
        assert COOKIE in r.cookies
        me = await client.get("/api/auth/me", cookies={COOKIE: r.cookies[COOKIE]})
        assert me.status_code == 200
        assert me.json()["email"] == 메일
        assert me.json()["workspace_name"] == "지황의 워크스페이스"

        async with AsyncSessionLocal() as db:
            u = (await db.execute(select(User).where(User.email == 메일))).scalar_one()
            assert u.password_hash and "abcdefgh" not in u.password_hash

    async def test_이메일은_소문자로_맞춘다(self, client):
        r = await client.post(
            "/api/auth/signup", json={"email": "  PW-Test@Test.local ", "password": "abcdefgh"}
        )
        assert r.status_code == 201
        assert r.json()["email"] == 메일

    async def test_중복_이메일은_409(self, client):
        await client.post("/api/auth/signup", json={"email": 메일, "password": "abcdefgh"})
        r = await client.post("/api/auth/signup", json={"email": 메일, "password": "zzzzzzzz"})
        assert r.status_code == 409

    async def test_구글_계정_이메일로는_가입_불가(self, client):
        """비밀번호를 붙여주면 그 이메일 주인이 아닌 사람이 계정을 가져간다."""
        async with AsyncSessionLocal() as db:
            db.add(User(email=구글메일, name="구글", google_id="sub-xyz"))
            await db.commit()
        r = await client.post("/api/auth/signup", json={"email": 구글메일, "password": "abcdefgh"})
        assert r.status_code == 409
        assert "Google" in r.json()["detail"]

    async def test_짧은_비밀번호_형식_틀린_이메일(self, client):
        r = await client.post("/api/auth/signup", json={"email": 메일, "password": "short"})
        assert r.status_code == 422
        r = await client.post("/api/auth/signup", json={"email": "not-an-email", "password": "abcdefgh"})
        assert r.status_code == 422


class TestLogin:
    async def test_맞으면_로그인(self, client):
        await client.post("/api/auth/signup", json={"email": 메일, "password": "abcdefgh"})
        r = await client.post("/api/auth/login", json={"email": 메일, "password": "abcdefgh"})
        assert r.status_code == 200 and COOKIE in r.cookies

    async def test_틀리면_이유를_가르지_않는다(self, client):
        await client.post("/api/auth/signup", json={"email": 메일, "password": "abcdefgh"})
        틀린비번 = await client.post("/api/auth/login", json={"email": 메일, "password": "wrongwrong"})
        없는메일 = await client.post(
            "/api/auth/login", json={"email": "nobody-" + 메일, "password": "abcdefgh"}
        )
        assert 틀린비번.status_code == 없는메일.status_code == 401
        assert 틀린비번.json()["detail"] == 없는메일.json()["detail"]

    async def test_구글_전용_계정은_비밀번호_로그인_안_됨(self, client):
        async with AsyncSessionLocal() as db:
            db.add(User(email=구글메일, name="구글", google_id="sub-xyz"))
            await db.commit()
        r = await client.post("/api/auth/login", json={"email": 구글메일, "password": "abcdefgh"})
        assert r.status_code == 401


class TestNoCrossLink:
    async def test_비밀번호_계정_이메일로_구글_로그인하면_이어붙이지_않는다(self):
        """가입 때 이메일 소유를 확인하지 않으므로, 남의 이메일로 먼저 가입해 둔 사람이
        진짜 주인의 Google 로그인과 한 계정에 묶이면 채널을 같이 보게 된다."""
        from app.api.auth import PasswordAccount, _upsert_user
        from app.services import google_oauth as goog

        async with AsyncSessionLocal() as db:
            db.add(User(email=메일, name="선점자", password_hash=hash_password("abcdefgh")))
            await db.commit()

        profile = goog.GoogleProfile(sub="sub-real-owner", email=메일, name="진짜", picture=None)
        async with AsyncSessionLocal() as db:
            with pytest.raises(PasswordAccount):
                await _upsert_user(db, profile)

        async with AsyncSessionLocal() as db:
            u = (await db.execute(select(User).where(User.email == 메일))).scalar_one()
            assert u.google_id is None, "구글 id 가 비밀번호 계정에 붙으면 안 된다"
