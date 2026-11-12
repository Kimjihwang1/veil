"""서버 안에서 도는 감시.

유튜브와 LLM 은 가짜로 바꿔 끼운다. 여기서 확인하는 건 세 가지 —
누구를 보는가, 새 것만 판별하는가, 하루 상한을 지키는가. 셋 중 하나가
틀리면 남의 채널을 보거나, 같은 댓글에 돈을 두 번 쓰거나, 폭주 영상 하나에
카드가 뚫린다.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.core.config import get_settings
from app.db.models import (
    Action,
    Channel,
    Comment,
    RiskAssessment,
    Video,
    Workspace,
)
from app.db.session import AsyncSessionLocal
from app.services import watcher
from app.services.collector import CollectedComment
from app.services.llm import LlmStats, LlmVerdict
from app.services.youtube_actions import NotConnected

pytestmark = pytest.mark.asyncio

접두 = "UC-watch-"


async def _purge():
    async with AsyncSessionLocal() as db:
        chans = list(
            (
                await db.execute(
                    select(Channel.id).where(Channel.youtube_channel_id.like(f"{접두}%"))
                )
            ).scalars().all()
        ) or [-1]
        cmts = list(
            (
                await db.execute(select(Comment.id).where(Comment.channel_id.in_(chans)))
            ).scalars().all()
        ) or [-1]
        wss = list(
            (
                await db.execute(select(Workspace.id).where(Workspace.name == "watch ws"))
            ).scalars().all()
        ) or [-1]
        await db.execute(delete(Action).where(Action.comment_id.in_(cmts)))
        await db.execute(delete(RiskAssessment).where(RiskAssessment.comment_id.in_(cmts)))
        await db.execute(delete(Comment).where(Comment.id.in_(cmts)))
        await db.execute(delete(Video).where(Video.channel_id.in_(chans)))
        await db.execute(delete(Channel).where(Channel.id.in_(chans)))
        await db.execute(delete(Workspace).where(Workspace.id.in_(wss)))
        await db.commit()


@pytest_asyncio.fixture
async def 세상():
    """채널 셋: 다 된 것 / 연동만 / 동의만. 첫 번째만 감시 대상이어야 한다."""
    await _purge()
    async with AsyncSessionLocal() as db:
        ws = Workspace(name="watch ws", type="personal")
        db.add(ws)
        await db.flush()
        둘다 = Channel(workspace_id=ws.id, youtube_channel_id=f"{접두}ok",
                     channel_title="둘 다", youtube_refresh_token="rt",
                     ai_consent_agreed=True)
        연동만 = Channel(workspace_id=ws.id, youtube_channel_id=f"{접두}tok",
                      channel_title="연동만", youtube_refresh_token="rt",
                      ai_consent_agreed=None)
        동의만 = Channel(workspace_id=ws.id, youtube_channel_id=f"{접두}yes",
                      channel_title="동의만", youtube_refresh_token=None,
                      ai_consent_agreed=True)
        db.add_all([둘다, 연동만, 동의만])
        await db.commit()
        ids = {"둘다": 둘다.id, "연동만": 연동만.id, "동의만": 동의만.id}
    # 하루 상한 카운터도 매 테스트 깨끗하게.
    watcher.상태.update(today=None, llm_calls_today=0, last_run_at=None, last_result=None)
    yield ids
    await _purge()


def _댓글(i: int, vid="vid-1", 상태=None, 앞="c") -> CollectedComment:
    return CollectedComment(
        youtube_comment_id=f"{접두}{앞}{i}", video_id=vid, content=f"댓글 {i}",
        author_name="a", author_channel_id=None,
        # 유튜브 최신순처럼 번호가 클수록 먼저 달린 댓글이다.
        published_at=(datetime.now(UTC) - timedelta(minutes=i)).replace(tzinfo=None), content_updated_at=None,
        like_count=0, total_reply_count=0, parent_comment_id=None,
        youtube_moderation=상태,
    )


class 가짜유튜브:
    """수집기 흉내. 어떤 채널을 물어봤는지 기록한다."""

    보류: list = []          # collect_held 가 돌려줄 것
    보류_물어본 = 0

    def __init__(self, key, 댓글수=2, access_token=None):
        self.물어본 = []
        self.댓글수 = 댓글수
        self.토큰 = access_token

        class S:
            quota_units = 7
        self.stats = S()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    async def resolve_channel(self, raw):
        self.물어본.append(raw)
        return {"id": raw}

    async def channel_videos(self, ch, limit):
        return [{"video_id": "vid-1", "title": "영상", "published_at": "2026-01-01T00:00:00Z"}]

    async def collect(self, video_id, max_comments=None, orders=("time", "relevance"), stop_at=None):
        가짜유튜브.받은_정렬 = orders
        가짜유튜브.받은_멈출곳 = set(stop_at or ())
        return [_댓글(i, video_id) for i in range(self.댓글수)]

    async def collect_held(self, video_id, max_comments=None):
        assert self.토큰, "보류 댓글은 채널 주인 토큰으로 만든 수집기로만 받는다"
        가짜유튜브.보류_물어본 += 1
        return list(가짜유튜브.보류)


class 가짜판별:
    """LLM 흉내. 몇 번 불렸는지 센다."""

    만든수 = 0

    def __init__(self, concurrency=8, max_calls=None, channel_context="", prompt_template=None):
        가짜판별.만든수 += 1
        self.stats = LlmStats()
        self.prompt_version = "test"

    async def judge(self, text, parent_text=None, video=None):
        self.stats.calls += 1
        return LlmVerdict("pass", label="safe", category="정상", confidence=0.9, reason="테스트")


@pytest.fixture
def 가짜들(monkeypatch):
    monkeypatch.setattr(watcher, "YouTubeCollector", 가짜유튜브)
    monkeypatch.setattr(watcher, "LlmJudge", 가짜판별)
    # .env 에 PREFILTER_MODE=on 이면 ML 이 가짜 댓글을 통과시켜 LLM 호출 수가
    # 바뀐다. 이 테스트는 LLM 상한·호출을 보므로 앞단을 끈다.
    monkeypatch.setattr(watcher, "get_prefilter", lambda: None)

    # 실제 DB 에 연동·동의된 채널이 있으면 그것까지 대상에 잡힌다. 그러면
    # 가짜 수집기가 실채널에 가짜 댓글을 넣는다 — 실제로 한 번 그랬다.
    # 이 테스트가 만든 채널만 보게 한다.
    원래 = watcher.대상_채널

    async def 우리것만(db):
        return [c for c in await 원래(db) if c.youtube_channel_id.startswith(접두)]
    monkeypatch.setattr(watcher, "대상_채널", 우리것만)

    async def 임베딩없음():
        return 0
    monkeypatch.setattr(watcher, "_임베딩", 임베딩없음)

    # 구글에 진짜로 토큰을 받으러 가지 않는다.
    async def 토큰(refresh):
        return "owner-token"
    monkeypatch.setattr(watcher, "access_token", 토큰)
    가짜유튜브.보류 = []
    가짜유튜브.보류_물어본 = 0

    cfg = get_settings()
    monkeypatch.setattr(cfg, "youtube_api_key", "가짜")
    monkeypatch.setattr(cfg, "llm_daily_cap", 3000)
    가짜판별.만든수 = 0


class TestTargets:
    async def test_연동과_동의가_둘_다_있어야_본다(self, 세상):
        async with AsyncSessionLocal() as db:
            ids = {c.id for c in await watcher.대상_채널(db)}
        assert 세상["둘다"] in ids
        assert 세상["연동만"] not in ids
        assert 세상["동의만"] not in ids


class TestOnce:
    async def test_새_댓글을_받아_판별해_큐에_넣는다(self, 세상, 가짜들):
        r = await watcher.watch_once()
        assert r["channels"] == 1
        assert r["collected"] == 2
        assert r["judged"] == 2
        assert r["errors"] == []

        async with AsyncSessionLocal() as db:
            상태들 = (
                await db.execute(
                    select(Comment.status).where(Comment.channel_id == 세상["둘다"])
                )
            ).scalars().all()
        # pending 으로 남은 게 없어야 한다.
        assert sorted(상태들) == ["passed", "passed"]

    async def test_두_번_돌아도_같은_댓글에_돈을_안_쓴다(self, 세상, 가짜들):
        await watcher.watch_once()
        r = await watcher.watch_once()
        # 두 번째 주기: 유튜브는 같은 댓글을 다시 주지만 DB 가 걸러서
        # 새 댓글 0, 판별 0 이어야 한다.
        assert r["collected"] == 0
        assert r["judged"] == 0
        # 판별할 게 없는 주기에 '채널 N 판별: TypeError' 가 남은 적이 있다 (2026-09-30).
        # 오류는 errors 에 조용히 쌓여서 이 assert 가 없으면 테스트가 못 잡는다.
        assert r["errors"] == []

    async def test_하루_상한을_넘기면_멈추고_pending_으로_남긴다(self, 세상, 가짜들, monkeypatch):
        monkeypatch.setattr(get_settings(), "llm_daily_cap", 1)
        r = await watcher.watch_once()
        assert r["collected"] == 2
        assert r["judged"] == 1            # 상한만큼만
        assert watcher.상태["llm_calls_today"] == 1

        async with AsyncSessionLocal() as db:
            pending = (
                await db.execute(
                    select(Comment).where(
                        Comment.channel_id == 세상["둘다"], Comment.status == "pending"
                    )
                )
            ).scalars().all()
        # 나머지 하나는 잃어버리지 않고 pending 으로 남아 다음 날 이어간다.
        assert len(pending) == 1

        r2 = await watcher.watch_once()
        assert r2["judged"] == 0
        assert any("상한" in s for s in r2["skipped"])

    async def test_날이_바뀌면_상한을_다시_센다(self, 세상, 가짜들):
        watcher.상태["today"] = "2000-01-01"
        watcher.상태["llm_calls_today"] = 999_999
        assert watcher._남은_오늘_호출() == get_settings().llm_daily_cap
        assert watcher.상태["llm_calls_today"] == 0

    async def test_상태를_남긴다(self, 세상, 가짜들):
        assert watcher.상태["last_run_at"] is None
        await watcher.watch_once()
        assert watcher.상태["last_run_at"] is not None
        assert watcher.상태["last_result"]["judged"] == 2

    async def test_유튜브_키가_없으면_조용히_넘어간다(self, 세상, 가짜들, monkeypatch):
        monkeypatch.setattr(get_settings(), "youtube_api_key", None)
        r = await watcher.watch_once()
        assert r["judged"] == 0
        assert any("YOUTUBE_API_KEY" in e for e in r["errors"])


class TestCollectOrder:
    async def test_최신순으로만_받고_이미_본_댓글에서_멈춘다(self, 세상, 가짜들):
        # 최신 200 + 인기 200 만 받으면 그 사이에 달린 댓글을 영영 놓친다.
        # 최신순으로 '지난번에 본 곳까지' 받아야 몇 개가 달렸든 다 들어온다.
        await watcher.watch_once()
        assert 가짜유튜브.받은_정렬 == ("time",)
        assert 가짜유튜브.받은_멈출곳 == set()            # 처음엔 아는 댓글이 없다

        await watcher.watch_once()
        assert 가짜유튜브.받은_멈출곳 == {f"{접두}c0", f"{접두}c1"}

    async def test_판별은_달린_순서대로(self, 세상, 가짜들, monkeypatch):
        # 상한에 걸리면 먼저 달린 댓글부터 판별해야 한다. DB 에 들어간 순서(최신순)로
        # 하면 오래 기다린 댓글이 계속 밀린다.
        monkeypatch.setattr(get_settings(), "llm_daily_cap", 1)
        await watcher.watch_once()
        async with AsyncSessionLocal() as db:
            남은 = (
                await db.execute(
                    select(Comment.youtube_comment_id).where(
                        Comment.channel_id == 세상["둘다"], Comment.status == "pending"
                    )
                )
            ).scalars().all()
        assert 남은 == [f"{접두}c0"]                       # 나중에 달린 것이 남는다


class TestHeld:
    """유튜브가 보류·스팸 의심으로 잡아 둔 댓글도 Veil 로 들어와야 한다 (2026-11-04)."""

    async def _한_줄(self, cid):
        async with AsyncSessionLocal() as db:
            return (
                await db.execute(select(Comment).where(Comment.youtube_comment_id == cid))
            ).scalar_one()

    async def test_보류_댓글은_AI가_정상이라_해도_검토_피드로_간다(self, 세상, 가짜들):
        # 통과로 두면 유튜브에선 계속 안 보이는데 아무도 안 본 댓글이 된다.
        가짜유튜브.보류 = [_댓글(9, 상태="heldForReview", 앞="h")]
        r = await watcher.watch_once()
        assert r["collected"] == 3 and r["errors"] == []

        c = await self._한_줄(f"{접두}h9")
        assert c.youtube_moderation == "heldForReview"
        assert c.status == "queued"
        assert (await self._한_줄(f"{접두}c0")).status == "passed"   # 평범한 댓글은 그대로

    async def test_공개_목록이_보류_표시를_지우지_않는다(self, 세상, 가짜들):
        # API 키로 받은 공개 목록은 상태값이 없다(None). 그걸로 덮으면 보류 표시가 사라진다.
        가짜유튜브.보류 = [_댓글(0, 상태="likelySpam")]       # 공개 목록의 c0 과 같은 댓글
        await watcher.watch_once()
        가짜유튜브.보류 = []
        await watcher.watch_once()
        assert (await self._한_줄(f"{접두}c0")).youtube_moderation == "likelySpam"

    async def test_수정돼서_보류된_댓글은_다시_판별해_검토_피드로(self, 세상, 가짜들):
        # 정상으로 통과한 댓글을 욕으로 고치면 유튜브가 보류한다. 옛 판정 '정상' 으로
        # 통과 탭에 두면 아무도 새 본문을 안 본다 (2026-11-04 테스트 채널에서 실제로).
        await watcher.watch_once()
        고친 = _댓글(0, 상태="heldForReview")
        고친.content = "바뀐 본문"
        가짜유튜브.보류 = [고친]
        r = await watcher.watch_once()
        assert r["judged"] == 1 and r["errors"] == []

        c = await self._한_줄(f"{접두}c0")
        assert c.content == "바뀐 본문"
        assert c.status == "queued"
        async with AsyncSessionLocal() as db:
            판정수 = len((await db.execute(
                select(RiskAssessment).where(RiskAssessment.comment_id == c.id)
            )).scalars().all())
        assert 판정수 == 2                                  # 옛 판정은 덮지 않고 남는다

    async def test_관리자가_처리한_댓글은_수정돼도_다시_판별하지_않는다(self, 세상, 가짜들):
        await watcher.watch_once()
        async with AsyncSessionLocal() as db:
            c = (await db.execute(
                select(Comment).where(Comment.youtube_comment_id == f"{접두}c0")
            )).scalar_one()
            c.reviewed_at = datetime.now(UTC).replace(tzinfo=None)
            await db.commit()
        고친 = _댓글(0, 상태="heldForReview")
        고친.content = "바뀐 본문"
        가짜유튜브.보류 = [고친]
        r = await watcher.watch_once()
        assert r["judged"] == 0
        assert (await self._한_줄(f"{접두}c0")).status == "passed"

    async def test_보관기한으로_지운_댓글은_다시_판별하지_않는다(self, 세상, 가짜들):
        from app.services.retention import MARK
        await watcher.watch_once()
        async with AsyncSessionLocal() as db:
            c = (await db.execute(
                select(Comment).where(Comment.youtube_comment_id == f"{접두}c0")
            )).scalar_one()
            c.content = MARK
            await db.commit()
        가짜유튜브.보류 = [_댓글(0, 상태="heldForReview")]
        r = await watcher.watch_once()
        assert r["judged"] == 0
        # 다시 받아 와도 지운 원문은 되살아나지 않는다 (YouTube API 정책, 30일)
        assert (await self._한_줄(f"{접두}c0")).content == MARK

    async def test_권한이_만료돼도_공개_댓글은_받는다(self, 세상, 가짜들, monkeypatch):
        async def 만료(refresh):
            raise NotConnected("만료")
        monkeypatch.setattr(watcher, "access_token", 만료)

        r = await watcher.watch_once()
        assert r["collected"] == 2
        assert 가짜유튜브.보류_물어본 == 0
        assert any("보류 댓글" in e and "다시 연결" in e for e in r["errors"])


class TestFetchNow:
    async def test_동시에_두_번_눌러도_한_번만_돈다(self, 세상, 가짜들):
        # 확인과 '도는 중' 표시 사이에 await 가 있으면 둘 다 통과해 같은 댓글을 두 번 판별한다.
        r1, r2 = await asyncio.gather(
            watcher.fetch_channel(세상["둘다"]), watcher.fetch_channel(세상["둘다"]),
            return_exceptions=True,
        )
        assert sum(isinstance(r, watcher.Busy) for r in (r1, r2)) == 1
        assert 세상["둘다"] not in watcher._돌고있는        # 끝나면 표시를 지운다
