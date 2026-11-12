"""연동된 채널을 서버가 스스로 감시한다.

전에는 scripts/watch.py 를 개발자가 터미널에서 돌려야 새 댓글이 들어왔다.
관리자한테는 터미널이 없다. 채널을 연결했으면 그걸로 끝이어야 하고, 한 시간
뒤에 화면을 열면 새 댓글이 위험도 순으로 줄 서 있어야 한다. 그래서 파기
(retention.py)와 같은 방식으로 서버가 켜져 있는 동안 혼자 돈다.

한 주기에 하는 일, 채널마다:

    ① 최신 영상 N개 목록                      (유튜브 1 unit)
    ② 영상마다 댓글 받기                       (영상당 2~3 units)
       + 유튜브가 보류·스팸 의심으로 잡아 둔 댓글 (영상당 2 units, 채널 주인 토큰으로)
    ③ DB 에 넣기 — 이미 있는 댓글은 그대로     (upsert)
    ④ 새로 들어온 것(pending)만 LLM 판별       (건당 약 0.17원)
    ⑤ 행선지대로 검토 큐 / 통과

③④ 덕에 같은 영상을 매시간 다시 긁어도 새 댓글이 0건이면 돈은 안 든다.

감시 대상은 '연동(리프레시 토큰 있음) + AI 판별 동의' 둘 다 된 채널만이다.
동의 없이 판별하면 정책 위반이고, 연동 안 된 채널은 조치를 못 하니 굳이
서버가 돈을 써서 볼 이유가 없다 — 그런 채널은 스크립트로 따로 돌린다.

돈이 새는 길 두 개를 막는다.
  - 실행당 상한 (llm_max_calls_per_run): 한 채널이 한 주기에 태울 수 있는 양
  - 하루 상한 (llm_daily_cap): 폭주 영상이 매시간 상한까지 태우는 걸 막는다.
    넘치면 그날은 멈추고, 댓글은 pending 으로 남아 다음 날 이어간다.
"""

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy import text as sq

from app.core.config import get_settings
from app.db.models import Channel, ChannelRule
from app.db.session import AsyncSessionLocal
from app.services.collector import YouTubeApiError, YouTubeCollector, save_comments, save_video
from app.services.llm import LlmJudge
from app.services.auto_hide import AutoHideStats, apply_auto_hides
from app.services.pipeline import QUEUE_INFO, QUEUE_JUDGE, process_many
from app.services.prefilter import get_prefilter
from app.services.store import save_results
from app.services.youtube_actions import NotConnected, access_token

log = logging.getLogger("outlier.watch")

# 서버가 막 떴을 때는 다른 일이 몰린다. 조금 기다렸다 시작한다.
FIRST_DELAY = 90

# 마지막으로 돈 결과. /api/health/watch 가 이걸 보여준다 — 관리자가
# "이거 돌고 있는 거 맞아?" 를 확인할 곳이 없으면 멈춰도 아무도 모른다.
상태: dict = {
    "last_run_at": None,
    "last_result": None,
    "today": None,          # 하루 상한을 세는 기준 날짜 (UTC)
    "llm_calls_today": 0,
}


def _오늘() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _남은_오늘_호출() -> int:
    """하루 상한에서 오늘 쓴 만큼 뺀 것. 날이 바뀌면 0 부터 다시 센다."""
    if 상태["today"] != _오늘():
        상태["today"] = _오늘()
        상태["llm_calls_today"] = 0
    return max(get_settings().llm_daily_cap - 상태["llm_calls_today"], 0)


async def 대상_채널(db) -> list[Channel]:
    """연동 + 동의 둘 다 된 채널만."""
    return list(
        (
            await db.execute(
                select(Channel).where(
                    Channel.workspace_id.isnot(None),
                    Channel.youtube_refresh_token.isnot(None),
                    Channel.ai_consent_agreed.is_(True),
                ).order_by(Channel.id)
            )
        ).scalars().all()
    )


async def _수집(yt, channel: Channel, cfg, owner=None) -> int:
    """채널 하나의 최신 영상 댓글을 받아 DB 에 넣는다. 새로 들어온 건수를 돌려준다.

    owner 는 채널 주인 토큰으로 만든 수집기다. 있으면 유튜브가 보류한 댓글도 받는다.
    """
    ch_info = await yt.resolve_channel(channel.youtube_channel_id)
    videos = await yt.channel_videos(ch_info, limit=cfg.watch_videos_per_channel)

    새로 = 0
    async with AsyncSessionLocal() as db:
        for v in videos:
            # 최신순으로 받다가 이미 저장한 최상위 댓글이 나오면 멈춘다.
            # 그 사이에 몇 개가 달렸든 전부 받는다. 전에는 최신 200 + 인기 200 만
            # 받아서, 한 시간에 그보다 많이 달리면 사이 것을 영영 놓쳤다.
            known = set(
                (
                    await db.execute(
                        sq("SELECT youtube_comment_id FROM comments "
                           "WHERE channel_id = :c AND video_id = :v AND parent_comment_id IS NULL"),
                        {"c": channel.id, "v": v["video_id"]},
                    )
                ).scalars()
            )
            try:
                comments = await yt.collect(
                    v["video_id"], max_comments=cfg.watch_per_video,
                    orders=("time",), stop_at=known,
                )
            except YouTubeApiError as e:
                if e.reason == "quotaExceeded":
                    raise
                log.warning("영상 %s 수집 실패 — %s", v["video_id"], e.reason)
                continue

            if owner is not None:
                try:
                    보류 = await owner.collect_held(v["video_id"], max_comments=cfg.watch_per_video)
                except YouTubeApiError as e:
                    if e.reason == "quotaExceeded":
                        raise
                    log.warning("영상 %s 보류 댓글 수집 실패 — %s", v["video_id"], e.reason)
                    보류 = []
                # 같은 댓글이 두 번 들어가면 upsert 한 번에 같은 행을 두 번 건드려
                # PostgreSQL 이 거부한다. 보류 쪽이 상태값을 갖고 있어 그쪽을 남긴다.
                합친 = {c.youtube_comment_id: c for c in comments}
                합친.update({c.youtube_comment_id: c for c in 보류})
                comments = list(합친.values())

            전 = (
                await db.execute(
                    sq("SELECT count(*) FROM comments WHERE channel_id = :c"),
                    {"c": channel.id},
                )
            ).scalar_one()

            # videos.list 를 따로 부르지 않는다 (영상당 1 unit 절약).
            # 목록에서 받은 제목·게시일로 충분하고, 채널은 이미 안다.
            await save_video(
                db, channel.id,
                {"id": v["video_id"],
                 "snippet": {"title": v["title"], "publishedAt": v["published_at"]},
                 "statistics": {}},
            )
            await save_comments(db, channel.id, comments)

            후 = (
                await db.execute(
                    sq("SELECT count(*) FROM comments WHERE channel_id = :c"),
                    {"c": channel.id},
                )
            ).scalar_one()
            새로 += 후 - 전
    return 새로


async def _판별(channel: Channel, 한도: int) -> tuple[int, AutoHideStats, int]:
    """이 채널의 pending 댓글을 한도 안에서 판별한다.

    (판별한 건수, 자동 숨김 결과, 검토 큐로 간 건수) 를 돌려준다.
    """
    if 한도 <= 0:
        return 0, AutoHideStats(), 0
    cfg = get_settings()
    limit = min(한도, cfg.llm_max_calls_per_run)

    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                sq("""
                    SELECT c.id, c.content, p.content
                    FROM comments c
                    LEFT JOIN comments p ON p.youtube_comment_id = c.parent_comment_id
                    WHERE c.channel_id = :cid AND c.status = 'pending'
                    ORDER BY c.published_at NULLS LAST, c.id
                    LIMIT :lim
                """),
                {"cid": channel.id, "lim": limit},
            )
        ).all()
        if not rows:
            return 0, AutoHideStats(), 0
        rules = (
            await db.execute(
                select(ChannelRule).where(
                    ChannelRule.channel_id == channel.id, ChannelRule.enabled.is_(True)
                )
            )
        ).scalars().all()
        ch = await db.get(Channel, channel.id)
        ctx, auto_hide = ch.context or "", ch.auto_hide_set

    judge = LlmJudge(
        concurrency=8, max_calls=len(rows) + 10, channel_context=ctx,
        prompt_template=ch.prompt_template,
    )
    results = await process_many(
        rules, judge, [(r[1], r[2]) for r in rows], auto_hide, prefilter=get_prefilter(),
    )

    pairs = [(r[0], v) for r, v in zip(rows, results)]
    async with AsyncSessionLocal() as db:
        await save_results(
            db, pairs, model=cfg.openai_model, prompt_version=judge.prompt_version,
        )
        # 차단어·자동 숨김 분류로 '숨김' 이 된 것을 유튜브에 실제로 반영한다.
        # 이게 없으면 우리 숨김 목록에만 들어가고 유튜브엔 그대로 보인다.
        hides = await apply_auto_hides(db, ch, pairs)

    상태["llm_calls_today"] += judge.stats.calls
    queued = sum(1 for _, v in pairs if v.destination in (QUEUE_JUDGE, QUEUE_INFO))
    return len(rows), hides, queued


async def _임베딩() -> int:
    """유사 사례 검색용 벡터. 매우 싸다 (1,000건에 1원 안 됨)."""
    from pgvector.sqlalchemy import Vector
    from sqlalchemy import bindparam

    from app.db.models import EMBEDDING_DIM, Comment
    from app.services.embed import Embedder

    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(Comment.id, Comment.content)
                .where(Comment.embedding.is_(None), Comment.content.isnot(None))
                .limit(500)
            )
        ).all()
        if not rows:
            return 0, AutoHideStats()
        vectors = await Embedder().embed([c for _, c in rows])
        t = Comment.__table__
        await db.execute(
            t.update()
            .where(t.c.id == bindparam("cid"))
            .values(embedding=bindparam("vec", type_=Vector(EMBEDDING_DIM))),
            [{"cid": cid, "vec": v} for (cid, _), v in zip(rows, vectors)],
        )
        await db.commit()
    return len(rows)


# 지금 돌고 있는 채널. 서버의 정기 감시와 관리자의 [조회] 가 같은 채널을 동시에
# 돌면 같은 댓글을 두 번 판별해 돈을 두 번 쓴다.
_돌고있는: set[int] = set()


def interval_min(channel: Channel) -> int:
    """이 채널의 감시 주기(분). 채널이 정하지 않았으면 서버 기본값."""
    return channel.watch_interval_min or max(get_settings().watch_interval // 60, 1)


def _때가_됐나(channel: Channel, now: datetime) -> bool:
    if channel.watch_enabled is False:
        return False
    last = channel.last_watched_at
    return last is None or (now - last).total_seconds() >= interval_min(channel) * 60


async def _주인_토큰(ch: Channel, 결과: dict) -> str | None:
    """보류 댓글은 채널 주인 자격으로만 받을 수 있다. 토큰이 죽었으면 None —
    그래도 공개 댓글 수집은 API 키로 계속한다."""
    if not ch.youtube_refresh_token:
        return None
    try:
        return await access_token(ch.youtube_refresh_token)
    except NotConnected:
        결과["errors"].append(f"채널 {ch.id} 보류 댓글: 유튜브 권한 만료, 다시 연결 필요")
    except Exception as e:
        결과["errors"].append(f"채널 {ch.id} 보류 댓글: {type(e).__name__}")
    return None


async def _한_채널(yt, ch: Channel, cfg, 결과: dict) -> bool:
    """채널 하나를 수집·판별한다. 쿼터가 끝나 더 돌 수 없으면 False."""
    _돌고있는.add(ch.id)
    try:
        try:
            토큰 = await _주인_토큰(ch, 결과)
            if 토큰:
                async with YouTubeCollector(cfg.youtube_api_key, access_token=토큰) as owner:
                    try:
                        새로 = await _수집(yt, ch, cfg, owner)
                    finally:
                        결과["quota_units"] += owner.stats.quota_units
            else:
                새로 = await _수집(yt, ch, cfg)
            결과["collected"] += 새로
        except YouTubeApiError as e:
            결과["errors"].append(f"채널 {ch.id}: {e.reason}")
            # 쿼터가 끝났으면 다른 채널도 안 된다.
            return e.reason != "quotaExceeded"
        except Exception as e:  # 한 채널이 죽어도 나머지는 본다
            결과["errors"].append(f"채널 {ch.id}: {type(e).__name__}")
            return True

        # 새 댓글이 없어도 pending 이 남아 있을 수 있다 (전 주기에 상한에
        # 걸렸거나, 동의 전에 수집만 해둔 것). 그래서 매번 본다.
        남은 = _남은_오늘_호출()
        if 남은 <= 0:
            결과["skipped"].append(f"채널 {ch.id}: 오늘 LLM 상한 도달")
            return True
        try:
            n, hides, queued = await _판별(ch, 남은)
            결과["judged"] += n
            결과["queued"] += queued
            결과["auto_hidden"] += hides.synced
            if hides.failed or hides.not_connected:
                결과["auto_hide_unsynced"] += hides.failed + hides.not_connected
            for note in hides.notes:
                결과["errors"].append(f"채널 {ch.id} 자동 숨김: {note}")
        except Exception as e:
            결과["errors"].append(f"채널 {ch.id} 판별: {type(e).__name__}")
        return True
    finally:
        _돌고있는.discard(ch.id)
        async with AsyncSessionLocal() as db:
            row = await db.get(Channel, ch.id)
            if row is not None:
                row.last_watched_at = datetime.now(UTC).replace(tzinfo=None)
                await db.commit()


def _빈_결과() -> dict:
    return {"channels": 0, "collected": 0, "judged": 0, "queued": 0, "embedded": 0,
            "auto_hidden": 0, "auto_hide_unsynced": 0,
            "quota_units": 0, "skipped": [], "errors": []}


async def watch_once(due_only: bool = False) -> dict:
    """한 주기. 무엇을 얼마나 했는지 돌려준다.

    due_only 면 '자동 감지' 가 켜져 있고 주기가 된 채널만 본다 (run_forever 가 1분마다
    부른다). 아니면 대상 채널을 전부 본다.
    """
    cfg = get_settings()
    결과 = _빈_결과()

    if not cfg.youtube_api_key:
        결과["errors"].append("YOUTUBE_API_KEY 없음")
        return 결과

    async with AsyncSessionLocal() as db:
        채널들 = await 대상_채널(db)
    if due_only:
        now = datetime.now(UTC).replace(tzinfo=None)
        채널들 = [c for c in 채널들 if _때가_됐나(c, now) and c.id not in _돌고있는]
    결과["channels"] = len(채널들)
    if not 채널들:
        return 결과

    async with YouTubeCollector(cfg.youtube_api_key) as yt:
        for ch in 채널들:
            if not await _한_채널(yt, ch, cfg, 결과):
                break
        결과["quota_units"] += yt.stats.quota_units

    if 결과["judged"] and cfg.openai_api_key:
        try:
            결과["embedded"] = await _임베딩()
        except Exception as e:
            결과["errors"].append(f"임베딩: {type(e).__name__}")

    상태["last_run_at"] = datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds")
    상태["last_result"] = 결과
    return 결과


class Busy(Exception):
    """이 채널을 이미 돌고 있다."""


async def fetch_channel(channel_id: int) -> dict:
    """관리자가 [조회] 를 눌렀다. 이 채널 하나만 지금 수집·판별한다.

    정기 감시와 달리 유튜브 권한이 없어도 돈다 — 수집은 API 키로 되고, 판별 결과를
    보는 건 권한과 무관하다. 동의는 반드시 있어야 한다 (호출하는 쪽이 확인한다).
    하루 LLM 상한은 정기 감시와 같이 센다.
    """
    cfg = get_settings()
    if channel_id in _돌고있는:
        raise Busy()
    # 확인과 표시 사이에 await 가 있으면 거의 동시에 들어온 두 번째 [조회] 도 확인을 통과해
    # 같은 pending 댓글을 두 번 판별한다 (돈도 두 번). 그래서 확인하자마자 바로 표시한다.
    _돌고있는.add(channel_id)
    try:
        결과 = _빈_결과()
        if not cfg.youtube_api_key:
            결과["errors"].append("YOUTUBE_API_KEY 없음")
            return 결과
        async with AsyncSessionLocal() as db:
            ch = await db.get(Channel, channel_id)
        결과["channels"] = 1
        async with YouTubeCollector(cfg.youtube_api_key) as yt:
            await _한_채널(yt, ch, cfg, 결과)
            결과["quota_units"] += yt.stats.quota_units
        return 결과
    finally:
        _돌고있는.discard(channel_id)


# 정기 감시가 깨는 간격. 채널마다 주기가 달라서, 1분마다 깨어 '때가 된' 채널만 돈다.
TICK = 60


async def run_forever() -> None:
    """서버가 사는 동안 주기적으로 돈다. 한 번 실패해도 멈추지 않는다."""
    cfg = get_settings()
    if not cfg.watch_enabled:
        log.info("감시 꺼짐 (WATCH_ENABLED=false)")
        return
    await asyncio.sleep(FIRST_DELAY)
    while True:
        try:
            r = await watch_once(due_only=True)
            if r["collected"] or r["judged"] or r["errors"]:
                log.info(
                    "감시 — 채널 %d · 새 댓글 %d · 판별 %d · 쿼터 %d · 오류 %s",
                    r["channels"], r["collected"], r["judged"],
                    r["quota_units"], r["errors"] or "없음",
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("감시 주기가 실패했다. 다음 주기에 다시 시도한다")
        await asyncio.sleep(TICK)
