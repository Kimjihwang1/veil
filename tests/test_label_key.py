"""라벨링 페이지 팀 비밀번호. HTTP 헤더는 ASCII 만 통과하므로 비밀번호는 영문·숫자로 정한다.

라벨링 페이지 팀 비밀번호. 서버에 올리면 주소만 알면 누구나 실제 댓글을 보게
되므로, LABEL_KEY 가 설정된 경우 헤더 없이는 401 이어야 한다."""

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.main import app

pytestmark = pytest.mark.asyncio


@pytest.fixture
def 키(monkeypatch):
    monkeypatch.setattr(get_settings(), "label_key", "team-pass-1")
    yield
    monkeypatch.setattr(get_settings(), "label_key", "")


async def test_키_없으면_401(키):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/api/label/labelers")
    assert r.status_code == 401


async def test_틀린_키도_401(키):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/api/label/labelers", headers={"X-Label-Key": "wrong"})
    assert r.status_code == 401


async def test_맞는_키면_통과(키):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/api/label/labelers", headers={"X-Label-Key": "team-pass-1"})
    assert r.status_code == 200


async def test_키가_비어_있으면_검사_안_함():
    assert get_settings().label_key == ""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/api/label/labelers")
    assert r.status_code == 200
