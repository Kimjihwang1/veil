"""AI 프롬프트 · 지금 조회 · 자동 감지 (app/api/channel_tools.py).

GPT 와 유튜브는 부르지 않는다. 가짜로 바꿔 끼운다.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.api import channel_tools
from app.db.models import Channel
from app.db.session import AsyncSessionLocal
from app.services import watcher
from app.services.llm import CHANNEL_SLOT, build_prompt, default_template, prompt_version
from tests.test_auth import as_, client, world  # noqa: F401  (픽스처)

pytestmark = pytest.mark.asyncio


async def _set(cid, **kw):
    async with AsyncSessionLocal() as db:
        ch = await db.get(Channel, cid)
        for k, v in kw.items():
            setattr(ch, k, v)
        await db.commit()


def test_고친_프롬프트에_채널_기준이_들어간다():
    t = f"짧은 지시문\n{CHANNEL_SLOT}"
    assert build_prompt("ㄹㅈㄷ 는 칭찬", t) == "짧은 지시문\nㄹㅈㄷ 는 칭찬"
    assert build_prompt("", t).endswith("(없음)")
    # 기본값 템플릿에 기준을 넣으면 예전 build_prompt 와 같아야 한다 — 저장 안 한 채널은 그대로다.
    assert default_template().replace(CHANNEL_SLOT, "기준") == build_prompt("기준")
    assert prompt_version("기준", t) != prompt_version("기준")


class TestPrompt:
    async def test_처음엔_기본값(self, client, world):
        r = await client.get(f"/api/channels/{world['a']['channel']}/prompt", cookies=as_(world["a"]["token"]))
        p = r.json()
        assert p["is_default"] and p["template"] == default_template()

    async def test_저장하면_지문이_바뀌고_기본값으로_되돌릴_수_있다(self, client, world):
        cid, tok = world["a"]["channel"], world["a"]["token"]
        before = (await client.get(f"/api/channels/{cid}/prompt", cookies=as_(tok))).json()["prompt_version"]
        r = await client.put(f"/api/channels/{cid}/prompt", json={"template": f"새 지시문 {CHANNEL_SLOT}"}, cookies=as_(tok))
        assert not r.json()["is_default"] and r.json()["prompt_version"] != before
        # 기본값을 그대로 저장하면 '고친 것 없음' 으로 둔다
        r = await client.put(f"/api/channels/{cid}/prompt", json={"template": default_template()}, cookies=as_(tok))
        assert r.json()["is_default"] and r.json()["prompt_version"] == before

    async def test_시험은_초안으로_돌고_저장하지_않는다(self, client, world, monkeypatch):
        cid, tok = world["a"]["channel"], world["a"]["token"]
        받은 = {}

        class 가짜판별:
            def __init__(self, **kw):
                받은.update(kw)
                self.stats = SimpleNamespace(calls=1)

            async def judge(self, text, video=None):
                받은["text"], 받은["video"] = text, video.title
                return SimpleNamespace(label="ambiguous", category="욕설", confidence=0.9, severity=None,
                                       reason="이유", raw='{"label":"ambiguous"}', error=None)

        monkeypatch.setattr(channel_tools, "LlmJudge", 가짜판별)
        monkeypatch.setattr(channel_tools.get_settings(), "openai_api_key", "가짜")
        channel_tools._시험.clear()
        r = await client.post(f"/api/channels/{cid}/prompt/test", cookies=as_(tok),
                              json={"text": "ㅅㅂ", "video_title": "라이브", "template": "초안"})
        d = r.json()
        assert r.status_code == 200
        assert 받은["prompt_template"] == "초안" and 받은["text"] == "ㅅㅂ" and 받은["video"] == "라이브"
        assert d["label"] == "ambiguous" and d["level"] == 3  # AI 값이 없어 표로 욕설=4, 애매라 한 단계 내림
        assert d["tests_left"] == channel_tools.TEST_DAILY - 1
        p = (await client.get(f"/api/channels/{cid}/prompt", cookies=as_(tok))).json()
        assert p["is_default"]  # 시험은 저장하지 않는다

    async def test_하루_시험_횟수를_넘기면_막는다(self, client, world, monkeypatch):
        cid = world["a"]["channel"]
        monkeypatch.setattr(channel_tools.get_settings(), "openai_api_key", "가짜")
        channel_tools._시험[(cid, channel_tools._오늘())] = channel_tools.TEST_DAILY
        r = await client.post(f"/api/channels/{cid}/prompt/test", json={"text": "x"}, cookies=as_(world["a"]["token"]))
        assert r.status_code == 429
        channel_tools._시험.clear()


class TestFetch:
    async def test_동의_전에는_못_불러온다(self, client, world):
        r = await client.post(f"/api/channels/{world['a']['channel']}/fetch", cookies=as_(world["a"]["token"]))
        assert r.status_code == 403

    async def test_방금_돌았으면_잠깐_막는다(self, client, world, monkeypatch):
        cid = world["a"]["channel"]
        await _set(cid, ai_consent_agreed=True, last_watched_at=datetime.now(UTC).replace(tzinfo=None))
        r = await client.post(f"/api/channels/{cid}/fetch", cookies=as_(world["a"]["token"]))
        assert r.status_code == 429

    async def test_이_채널만_돈다(self, client, world, monkeypatch):
        cid = world["a"]["channel"]
        await _set(cid, ai_consent_agreed=True)
        돈_채널 = []

        async def 가짜(channel_id):
            돈_채널.append(channel_id)
            return {"collected": 3, "judged": 3, "queued": 1, "errors": []}

        monkeypatch.setattr(watcher, "fetch_channel", 가짜)
        r = await client.post(f"/api/channels/{cid}/fetch", cookies=as_(world["a"]["token"]))
        assert r.json() == {"collected": 3, "judged": 3, "queued": 1, "errors": []}
        assert 돈_채널 == [cid]

    async def test_이미_도는_중이면_409(self, client, world, monkeypatch):
        cid = world["a"]["channel"]
        await _set(cid, ai_consent_agreed=True)
        watcher._돌고있는.add(cid)
        try:
            r = await client.post(f"/api/channels/{cid}/fetch", cookies=as_(world["a"]["token"]))
            assert r.status_code == 409
        finally:
            watcher._돌고있는.discard(cid)


class TestWatch:
    async def test_주기는_5분에서_24시간(self, client, world):
        cid, tok = world["a"]["channel"], world["a"]["token"]
        assert (await client.put(f"/api/channels/{cid}/watch", json={"interval_min": 4}, cookies=as_(tok))).status_code == 422
        assert (await client.put(f"/api/channels/{cid}/watch", json={"interval_min": 1441}, cookies=as_(tok))).status_code == 422
        r = await client.put(f"/api/channels/{cid}/watch", json={"interval_min": 30, "enabled": False}, cookies=as_(tok))
        assert r.json()["interval_min"] == 30 and r.json()["enabled"] is False
        assert r.json()["next_run_at"] is None  # 꺼져 있으면 다음 실행이 없다

    async def test_권한과_동의가_없으면_대상이_아니라고_알린다(self, client, world):
        r = await client.get(f"/api/channels/{world['a']['channel']}/watch", cookies=as_(world["a"]["token"]))
        assert r.json()["eligible"] is False


def test_때가_된_채널만_돈다():
    now = datetime(2026, 11, 3, 12, 0)
    ch = SimpleNamespace(watch_enabled=True, watch_interval_min=30, last_watched_at=now - timedelta(minutes=29))
    assert not watcher._때가_됐나(ch, now)
    ch.last_watched_at = now - timedelta(minutes=30)
    assert watcher._때가_됐나(ch, now)
    ch.last_watched_at = None
    assert watcher._때가_됐나(ch, now)  # 한 번도 안 돌았으면 바로
    ch.watch_enabled = False
    assert not watcher._때가_됐나(ch, now)
