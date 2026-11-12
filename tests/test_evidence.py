"""증거 보관함 (2026-11-09).

지키려는 것 넷.
  1. 심한 댓글은 판별하는 순간 남는다 — 나중엔 늦을 수 있다 (고치거나 숨기면 원문이 사라진다).
  2. 한 번 남긴 것은 안 바뀐다. 작성자가 고치면 고치기 전 글이 그대로 남는다.
  3. 남의 채널 증거는 보지도 지우지도 못한다.
  4. 30일이 지나면 지운다 (유튜브 API 정책).
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from app.db.models import Comment, Evidence, RiskAssessment
from app.db.session import AsyncSessionLocal
from app.services import retention
from app.services.evidence import capture, content_hash
from app.services.pipeline import QUEUE_JUDGE, PipelineResult
from app.services.store import save_results
from tests.test_auth import as_, client, world  # noqa: F401  (pytest 픽스처)

pytestmark = pytest.mark.asyncio


def _결과(label="harmful", category="위협", severity=5):
    return PipelineResult(
        text="", destination=QUEUE_JUDGE, decided_by="llm", reason="해치겠다고 한다",
        llm_label=label, llm_category=category, llm_severity=severity,
    )


async def _증거(comment_id):
    async with AsyncSessionLocal() as db:
        return list(
            (
                await db.execute(
                    select(Evidence).where(Evidence.comment_id == comment_id).order_by(Evidence.id)
                )
            ).scalars().all()
        )


async def _본문(comment_id, text):
    async with AsyncSessionLocal() as db:
        await db.execute(update(Comment).where(Comment.id == comment_id).values(content=text))
        await db.commit()


class TestAuto:
    async def test_위험도_4_이상_유해는_판별하는_순간_남는다(self, world):
        cid = world["a"]["comment"]
        async with AsyncSessionLocal() as db:
            await save_results(db, [(cid, _결과())], model="m", prompt_version="v")
        [e] = await _증거(cid)
        assert e.source == "auto" and e.saved_by is None
        assert e.content == "a 의 비밀 댓글"
        assert (e.label, e.category, e.severity) == ("harmful", "위협", 5)
        assert e.content_hash == content_hash("cmt-a", None, None, "a 의 비밀 댓글")
        assert e.expires_at is not None

    async def test_3_이하나_애매는_자동으로_남기지_않는다(self, world):
        cid = world["a"]["comment"]
        async with AsyncSessionLocal() as db:
            await save_results(db, [(cid, _결과(category="모욕", severity=3))])
            await save_results(db, [(cid, _결과(label="ambiguous", severity=5))])
        assert await _증거(cid) == []

    async def test_판정의_위험도도_같이_저장된다(self, world):
        cid = world["a"]["comment"]
        async with AsyncSessionLocal() as db:
            await save_results(db, [(cid, _결과(category="모욕", severity=2))])
            ra = (
                await db.execute(
                    select(RiskAssessment).where(RiskAssessment.comment_id == cid)
                    .order_by(RiskAssessment.id.desc()).limit(1)
                )
            ).scalar_one()
        assert ra.severity == 2


class TestSnapshot:
    async def test_작성자가_고쳐도_고치기_전_글이_남는다(self, world):
        cid = world["a"]["comment"]
        async with AsyncSessionLocal() as db:
            await capture(db, [cid], source="manual")
            await db.commit()
        await _본문(cid, "고친 글")
        async with AsyncSessionLocal() as db:
            await capture(db, [cid], source="manual")
            await capture(db, [cid], source="manual")   # 같은 글은 두 번 안 생긴다
            await db.commit()
        assert [e.content for e in await _증거(cid)] == ["a 의 비밀 댓글", "고친 글"]

    async def test_보관기한이_지나_지운_댓글은_남길_게_없다(self, world):
        cid = world["a"]["comment"]
        await _본문(cid, retention.MARK)
        async with AsyncSessionLocal() as db:
            assert await capture(db, [cid], source="manual") == []


class TestApi:
    async def test_저장하면_보관함에_뜨고_다시_눌러도_하나(self, client, world):
        a = world["a"]
        r1 = await client.post(f"/api/comments/{a['comment']}/evidence",
                               json={"note": "고소용"}, cookies=as_(a["token"]))
        assert r1.status_code == 200
        d = r1.json()
        assert d["source"] == "manual" and d["saved_by"] == "a@test.local" and d["note"] == "고소용"

        r2 = await client.post(f"/api/comments/{a['comment']}/evidence", cookies=as_(a["token"]))
        assert r2.status_code == 200 and r2.json()["id"] == d["id"]

        lst = (await client.get(f"/api/channels/{a['channel']}/evidence",
                                cookies=as_(a["token"]))).json()
        assert [x["id"] for x in lst] == [d["id"]]
        assert lst[0]["days_left"] is not None

        q = (await client.get(f"/api/channels/{a['channel']}/queue", cookies=as_(a["token"]))).json()
        assert q[0]["evidence"] is True

    async def test_원문이_지워진_댓글은_410(self, client, world):
        a = world["a"]
        await _본문(a["comment"], retention.MARK)
        r = await client.post(f"/api/comments/{a['comment']}/evidence", cookies=as_(a["token"]))
        assert r.status_code == 410

    async def test_남의_것은_저장도_삭제도_못_한다(self, client, world):
        a, b = world["a"], world["b"]
        r = await client.post(f"/api/comments/{a['comment']}/evidence", cookies=as_(b["token"]))
        assert r.status_code == 404

        eid = (await client.post(f"/api/comments/{a['comment']}/evidence",
                                 cookies=as_(a["token"]))).json()["id"]
        assert (await client.delete(f"/api/evidence/{eid}", cookies=as_(b["token"]))).status_code == 404
        assert len(await _증거(a["comment"])) == 1
        assert (await client.delete(f"/api/evidence/{eid}", cookies=as_(a["token"]))).status_code == 204
        assert await _증거(a["comment"]) == []

    async def test_CSV는_엑셀용_BOM에_수식을_막는다(self, client, world):
        a = world["a"]
        await _본문(a["comment"], '=HYPERLINK("http://x")')
        await client.post(f"/api/comments/{a['comment']}/evidence", cookies=as_(a["token"]))
        r = await client.get(f"/api/channels/{a['channel']}/evidence.csv", cookies=as_(a["token"]))
        assert r.status_code == 200
        assert r.content.startswith("﻿번호,".encode("utf-8"))
        assert "'=HYPERLINK" in r.text            # 엑셀이 수식으로 실행하지 않게
        assert "attachment" in r.headers["content-disposition"]

    async def test_인쇄용_화면은_원문을_그대로_escape(self, client, world):
        a = world["a"]
        await _본문(a["comment"], "<script>alert(1)</script>")
        await client.post(f"/api/comments/{a['comment']}/evidence", cookies=as_(a["token"]))
        r = await client.get(f"/api/channels/{a['channel']}/evidence/report", cookies=as_(a["token"]))
        assert r.status_code == 200
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in r.text
        assert "<script>alert" not in r.text


class TestQueueLevel:
    async def test_큐는_AI가_매긴_위험도_순(self, client, world):
        a = world["a"]
        async with AsyncSessionLocal() as db:
            ids = []
            for i, (label, sev) in enumerate([("harmful", 2), ("harmful", 5), ("ambiguous", 5)]):
                c = Comment(channel_id=a["channel"], youtube_comment_id=f"cmt-a-lv{i}",
                            content=f"단계 {i}", status="queued")
                db.add(c)
                await db.flush()
                db.add(RiskAssessment(comment_id=c.id, stage="llm", destination="queue_judge",
                                      risk_level=label, category="모욕", severity=sev))
                ids.append(c.id)
            await db.commit()
        q = (await client.get(f"/api/channels/{a['channel']}/queue", cookies=as_(a["token"]))).json()
        got = [(x["comment_id"], x["level"]) for x in q]
        # 5(유해) → 4(애매 5에서 한 단계) → 3(값 없는 옛 판정, 모욕 표) → 2
        assert got == [(ids[1], 5), (ids[2], 4), (a["comment"], 3), (ids[0], 2)]


class TestRetention:
    async def test_기한이_지난_증거는_행째_지운다(self, world):
        cid = world["a"]["comment"]
        async with AsyncSessionLocal() as db:
            await capture(db, [cid], source="manual")
            await db.commit()
        await _본문(cid, "고친 글")
        async with AsyncSessionLocal() as db:
            await capture(db, [cid], source="manual")
            await db.commit()
        old, new = await _증거(cid)
        async with AsyncSessionLocal() as db:
            await db.execute(
                update(Evidence).where(Evidence.id == old.id)
                .values(expires_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(days=1))
            )
            await db.commit()
        지움 = await retention.purge_once()
        assert 지움["증거"] >= 1
        assert [e.id for e in await _증거(cid)] == [new.id]
