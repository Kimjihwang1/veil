"""YouTube 댓글 수집 (F_R_107).

commentThreads 만 부르면 답글의 58%밖에 못 받는다 (실측). 스레드당 5개 상한이
걸려 있어서 답글이 많은 스레드일수록 심하게 잘린다 — 답글 23개짜리는 5개만 왔다.
답글이 많다는 건 논쟁이 붙었다는 뜻이고 악플이 있을 확률이 높은 곳이라,
하필 가장 중요한 데를 버리는 셈이 된다. 그래서 잘린 스레드만 골라 보충한다.

수집만 한다. 판별(1차 규칙·LLM)은 여기서 부르지 않는다.
"""

import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

API_BASE = "https://www.googleapis.com/youtube/v3"

# upsert 한 번에 보낼 행 수. PostgreSQL 의 쿼리당 파라미터 상한(32,767) 때문에
# 무한정 넣을 수 없다. 컬럼 16개 기준 2,047행이 한계라 여유를 두고 잡았다.
UPSERT_CHUNK = 1000

# 유튜브가 공개하지 않고 잡아 둔 댓글의 moderationStatus 값.
# 스튜디오 댓글 화면의 Held for review / Likely spam 에 해당한다.
HELD = ("heldForReview", "likelySpam")

# 원문·작성자 정보 보관 기간. 이후엔 위험도 등 파생 지표만 남긴다 (YouTube API 정책).
RETENTION_DAYS = 30

# 구글 reason 코드는 그대로는 원인 파악이 안 돼서 해석을 붙인다.
HINTS = {
    "keyInvalid": ".env의 YOUTUBE_API_KEY 값이 잘못됐다.",
    "ipRefererBlocked": (
        "키에 HTTP 리퍼러 제한이 걸려 있다. 서버에서는 리퍼러가 없어 막힌다. "
        "Cloud Console > 사용자 인증 정보 > 애플리케이션 제한사항을 '없음'으로."
    ),
    "accessNotConfigured": "이 프로젝트에서 YouTube Data API v3 사용 설정이 안 됐다.",
    "quotaExceeded": "오늘 쿼터(기본 10,000 units)를 다 썼다. 태평양시 자정에 리셋.",
    "commentsDisabled": "이 영상은 댓글이 꺼져 있다.",
    "videoNotFound": "영상 ID가 잘못됐거나 비공개/삭제된 영상.",
    "forbidden": "권한 거부. 키 제한 설정 또는 비공개 영상.",
}

_ID_PATTERNS = (
    r"(?:v=|/shorts/|youtu\.be/|/embed/)([A-Za-z0-9_-]{11})",
    r"^([A-Za-z0-9_-]{11})$",
)


class YouTubeApiError(RuntimeError):
    def __init__(self, status: int, reason: str, message: str):
        self.status = status
        self.reason = reason
        hint = HINTS.get(reason, "")
        text = f"[{status} {reason}] {message}"
        super().__init__(text + (f"\n  >>> {hint}" if hint else ""))


def extract_video_id(raw: str) -> str:
    """URL이든 ID든 받아서 영상 ID(11자)만 뽑는다."""
    for pattern in _ID_PATTERNS:
        if m := re.search(pattern, raw):
            return m.group(1)
    raise ValueError(f"영상 ID를 찾을 수 없다: {raw}")


def _parse_ts(value: str | None) -> datetime | None:
    """ISO8601('...Z') -> naive UTC.

    DB 컬럼이 timezone 없는 DateTime이라 맞춰준다. aware 를 그대로 넣으면
    asyncpg가 거부한다.
    """
    if not value:
        return None
    return datetime.fromisoformat(value).astimezone(UTC).replace(tzinfo=None)


@dataclass
class CollectedComment:
    youtube_comment_id: str
    video_id: str
    content: str
    author_name: str | None
    author_channel_id: str | None
    published_at: datetime | None
    content_updated_at: datetime | None
    like_count: int
    # 최상위 댓글에만 의미가 있다. 논쟁 강도 신호로 쓴다.
    total_reply_count: int
    # 답글이면 부모 댓글 ID. 3차 LLM에 맥락으로 넣어줘야 "그만해라" 같은
    # 댓글을 판별할 수 있다 — 부모 없이는 사람도 판단 못 한다.
    parent_comment_id: str | None
    # 유튜브 검토 상태. 채널 주인 토큰으로 받을 때만 온다 (API 키면 None).
    youtube_moderation: str | None = None

    @property
    def is_reply(self) -> bool:
        return self.parent_comment_id is not None


@dataclass
class CollectStats:
    """무엇을 어떻게 받았고 쿼터를 얼마나 썼는지."""

    threads: int = 0
    replies_inline: int = 0        # part=replies 로 공짜로 딸려온 답글
    replies_supplemented: int = 0  # 잘려서 따로 받아온 답글
    truncated_threads: int = 0     # 보충이 필요했던 스레드 수
    quota_units: int = 0
    comments_disabled: bool = False


def _to_comment(
    snippet: dict,
    comment_id: str,
    *,
    total_replies: int = 0,
    parent: str | None = None,
) -> CollectedComment:
    return CollectedComment(
        youtube_comment_id=comment_id,
        video_id=snippet.get("videoId", ""),
        # textDisplay 가 아니라 textOriginal 을 쓴다. textDisplay 는 링크가
        # <a> 태그로 들어 있어서 정규식 매칭에 넣으면 오탐이 난다.
        content=snippet.get("textOriginal", ""),
        author_name=snippet.get("authorDisplayName"),
        author_channel_id=(snippet.get("authorChannelId") or {}).get("value"),
        published_at=_parse_ts(snippet.get("publishedAt")),
        content_updated_at=_parse_ts(snippet.get("updatedAt")),
        like_count=snippet.get("likeCount", 0),
        total_reply_count=total_replies,
        parent_comment_id=parent,
        youtube_moderation=snippet.get("moderationStatus"),
    )


class YouTubeCollector:
    """영상 하나의 댓글을 답글까지 빠짐없이 받아온다.

    client 를 주입받는 이유: 테스트에서 httpx.MockTransport 로 갈아끼워
    네트워크·쿼터 없이 페이지네이션과 답글 보충 로직을 검증하기 위해서다.

    access_token 을 주면 API 키 대신 채널 주인 자격으로 부른다. 유튜브가 보류한
    댓글(collect_held)은 이 자격으로만 받을 수 있다.
    """

    def __init__(
        self,
        api_key: str | None,
        client: httpx.AsyncClient | None = None,
        access_token: str | None = None,
    ):
        self._key = api_key
        self._token = access_token
        self._client = client
        self._owns_client = client is None
        self.stats = CollectStats()

    async def __aenter__(self) -> "YouTubeCollector":
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=API_BASE, timeout=20)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    async def _call(self, path: str, **params) -> dict:
        headers = None
        if self._token:
            headers = {"Authorization": f"Bearer {self._token}"}
        else:
            params["key"] = self._key
        r = await self._client.get(f"/{path}", params=params, headers=headers)
        self.stats.quota_units += 1

        if r.is_success:
            return r.json()

        body = r.json().get("error", {})
        reason = (body.get("errors") or [{}])[0].get("reason", "unknown")
        raise YouTubeApiError(r.status_code, reason, body.get("message", ""))

    async def _pages(self, path: str, **params) -> AsyncIterator[dict]:
        """nextPageToken 이 없을 때까지 이어서 받는다."""
        token = None
        while True:
            extra = {"pageToken": token} if token else {}
            page = await self._call(path, **params, **extra)
            yield page
            token = page.get("nextPageToken")
            if not token:
                return

    async def _all_replies(self, parent_id: str) -> list[CollectedComment]:
        """comments.list 로 한 스레드의 답글을 전부 받는다."""
        out: list[CollectedComment] = []
        async for page in self._pages(
            "comments",
            part="snippet",
            parentId=parent_id,
            maxResults=100,
            textFormat="plainText",
        ):
            out += [
                _to_comment(it["snippet"], it["id"], parent=parent_id)
                for it in page.get("items", [])
            ]
        return out

    async def _collect_thread(self, thread: dict) -> list[CollectedComment]:
        self.stats.threads += 1
        top = thread["snippet"]["topLevelComment"]
        declared = thread["snippet"]["totalReplyCount"]
        inline = thread.get("replies", {}).get("comments", [])

        out = [_to_comment(top["snippet"], top["id"], total_replies=declared)]

        if declared > len(inline):
            # 잘렸다. 이 스레드만 comments.list 로 다시 받는다 (1 unit).
            self.stats.truncated_threads += 1
            replies = await self._all_replies(thread["id"])
            self.stats.replies_supplemented += len(replies)
        else:
            replies = [
                _to_comment(it["snippet"], it["id"], parent=thread["id"])
                for it in inline
            ]
            self.stats.replies_inline += len(replies)

        return out + replies

    async def _collect_order(
        self,
        video_id: str,
        order: str,
        max_comments: int | None,
        collected: dict[str, CollectedComment],
        stop_at: set[str] | None = None,
    ) -> None:
        """한 가지 정렬로 훑어 collected 에 누적한다. 중복은 키로 자동 제거된다.

        stop_at 에 든 스레드(이미 저장한 최상위 댓글)가 나온 페이지에서 멈춘다.
        최신순으로 받을 때 '지난번에 본 곳까지' 만 받는 용도다.
        """
        start = len(collected)
        async for page in self._pages(
            "commentThreads",
            # replies 를 붙여도 쿼터는 1 unit 그대로다. 안 붙일 이유가 없다.
            part="snippet,replies",
            videoId=video_id,
            maxResults=100,
            order=order,
            textFormat="plainText",
        ):
            seen_before = False
            for thread in page.get("items", []):
                if stop_at and thread["id"] in stop_at:
                    seen_before = True
                for c in await self._collect_thread(thread):
                    # comments.list 응답 snippet 에는 videoId 가 없다.
                    # 보충한 답글이 video_id 빈 값으로 저장돼서 영상별 조회에
                    # 안 잡히는 문제가 실제로 났다. 지금 어느 영상을 수집 중인지
                    # 아니까 API 응답에 기대지 말고 여기서 일괄로 채운다.
                    c.video_id = video_id
                    collected[c.youtube_comment_id] = c

            if seen_before:
                break
            if max_comments and len(collected) - start >= max_comments:
                break

    async def collect(
        self,
        video_id: str,
        max_comments: int | None = None,
        orders: tuple[str, ...] = ("time", "relevance"),
        stop_at: set[str] | None = None,
    ) -> list[CollectedComment]:
        """최상위 댓글 + 답글 전부.

        정렬 기준마다 따로 훑어서 합친다. 한 가지로만 받으면 표본이 치우친다.
        time 만 쓰면 상한에 걸려 '뒤늦게 달린 댓글'만 남고, relevance 만 쓰면
        좋아요를 많이 받은 정제된 여론만 남는다. 논란 영상일수록 두 집단이
        확연히 다르므로 둘 다 받아 합친다 (겹치는 건 알아서 하나가 된다).

        max_comments 는 비용 안전장치다. 정렬 기준별로 나눠 쓴다.
        스레드 중간에 끊으면 답글이 반쪽만 남으므로 페이지 경계에서만
        멈춘다 (약간 초과할 수 있다).
        """
        collected: dict[str, CollectedComment] = {}
        per_order = max_comments // len(orders) if max_comments else None

        for order in orders:
            try:
                await self._collect_order(video_id, order, per_order, collected, stop_at)
            except YouTubeApiError as e:
                # 댓글을 꺼둔 영상은 채널 단위로 돌릴 때 흔하다. 여기서 죽으면
                # 나머지 영상 수집이 통째로 중단되므로 빈 결과로 넘긴다.
                if e.reason != "commentsDisabled":
                    raise
                self.stats.comments_disabled = True
                break

        return list(collected.values())

    async def collect_held(
        self, video_id: str, max_comments: int | None = None
    ) -> list[CollectedComment]:
        """유튜브가 공개하지 않고 잡아 둔 댓글 — 보류(heldForReview) + 스팸 의심(likelySpam).

        공개 목록(collect)에는 안 나온다. 채널 주인만 스튜디오에서 보던 것이다.
        moderationStatus 는 주인 자격 요청에만 허용돼서 access_token 이 있어야 한다.

        스레드는 최상위나 답글 중 하나만 보류돼도 통째로 온다. 어느 댓글이 보류된
        건지는 댓글마다 붙어 오는 moderationStatus 로 구분된다 (공개된 건 published).

        공개 목록과 달리 '이미 본 곳에서 멈추기' 를 하지 않는다. 보류 목록은 시간순으로
        쌓이는 게 아니라 누가 처리할 때까지 남아 있는 목록이라 매번 처음부터 받는다.
        상태별로 페이지당 1 unit 이 든다.
        """
        if not self._token:
            raise RuntimeError("보류 댓글은 채널 주인 토큰(access_token)으로만 받을 수 있다")
        collected: dict[str, CollectedComment] = {}
        for status in HELD:
            start = len(collected)
            async for page in self._pages(
                "commentThreads",
                part="snippet,replies",
                videoId=video_id,
                moderationStatus=status,
                maxResults=100,
                textFormat="plainText",
            ):
                for thread in page.get("items", []):
                    for c in await self._collect_thread(thread):
                        c.video_id = video_id
                        collected[c.youtube_comment_id] = c
                if max_comments and len(collected) - start >= max_comments:
                    break
        return list(collected.values())

    async def resolve_channel(self, raw: str) -> dict:
        """@핸들 · UC아이디 · 채널 URL 아무거나 받아서 채널 정보를 돌려준다."""
        raw = raw.strip().rstrip("/")
        if "/" in raw:  # URL 이면 마지막 조각만 쓴다
            raw = raw.rsplit("/", 1)[-1]

        key = "id" if raw.startswith("UC") and len(raw) == 24 else "forHandle"
        if key == "forHandle" and not raw.startswith("@"):
            raw = "@" + raw

        data = await self._call("channels", part="snippet,contentDetails", **{key: raw})
        if not data.get("items"):
            raise YouTubeApiError(404, "channelNotFound", f"채널 없음: {raw}")
        return data["items"][0]

    async def channel_videos(self, channel: dict, limit: int = 10) -> list[dict]:
        """채널의 최신 영상 목록.

        search.list 는 호출당 100 units 라 몇 번만 써도 하루 한도가 찬다.
        업로드 재생목록(playlistItems)은 1 unit 이라 100배 싸다.
        """
        uploads = channel["contentDetails"]["relatedPlaylists"]["uploads"]
        out: list[dict] = []
        async for page in self._pages(
            "playlistItems", part="snippet,contentDetails",
            playlistId=uploads, maxResults=min(50, limit),
        ):
            for it in page.get("items", []):
                out.append({
                    "video_id": it["contentDetails"]["videoId"],
                    "title": it["snippet"]["title"],
                    "published_at": it["snippet"]["publishedAt"],
                })
                if len(out) >= limit:
                    return out
        return out

    async def video_info(self, video_id: str) -> dict:
        """영상이 속한 채널을 알아야 comments.channel_id 를 채울 수 있다."""
        data = await self._call("videos", part="snippet,statistics", id=video_id)
        if not data.get("items"):
            raise YouTubeApiError(404, "videoNotFound", f"영상 없음: {video_id}")
        return data["items"][0]


# ── DB 저장 ──────────────────────────────────────────────────────────


async def save_video(
    db, channel_id: int, info: dict, topic: str | None = None
) -> None:
    """videos.list 응답 한 건을 videos 테이블에 upsert 한다.

    topic 은 API 가 주는 값이 아니라 수집할 때 사람이 붙이는 라벨이다.
    재수집 시 덮어쓴다 — 주제를 나중에 고칠 수 있어야 하기 때문.
    """
    from sqlalchemy.dialects.postgresql import insert

    from app.db.models import Video

    snip = info.get("snippet", {})
    stats = info.get("statistics", {})
    count = stats.get("commentCount")

    row = {
        "video_id": info["id"],
        "channel_id": channel_id,
        "title": snip.get("title"),
        "topic": topic,
        "comment_count": int(count) if count is not None else None,
        "published_at": _parse_ts(snip.get("publishedAt")),
        "collected_at": datetime.now(UTC).replace(tzinfo=None),
    }
    stmt = insert(Video).values([row])
    await db.execute(
        stmt.on_conflict_do_update(
            index_elements=["video_id"],
            set_={
                "title": stmt.excluded.title,
                "topic": stmt.excluded.topic,
                "comment_count": stmt.excluded.comment_count,
                "collected_at": stmt.excluded.collected_at,
            },
        )
    )


async def save_comments(db, channel_id: int, comments: list[CollectedComment]) -> int:
    """수집 결과를 comments 테이블에 upsert 한다.

    upsert 인 이유: 같은 영상을 다시 돌려도 중복이 안 생기고, 댓글이 수정되거나
    좋아요가 변한 것만 갱신된다. 재수집이 안전해야 파일럿을 여러 번 돌릴 수 있다.

    본문이 바뀐 댓글은 판별 전(pending)으로 돌려 다시 판별한다. 정상으로 통과한
    댓글을 욕으로 고치면 유튜브는 보류하는데, 우리는 옛 판정 '정상' 으로 통과 탭에
    두고 있었다 (2026-11-04 테스트 채널). 관리자가 이미 처리한 댓글은 건드리지
    않는다 — 사람 판단 우선. 보관기한이 지나 지운 댓글도 아니다.
    """
    from sqlalchemy import and_, case, func, null
    from sqlalchemy.dialects.postgresql import insert

    from app.db.models import Comment
    from app.services.retention import MARK

    if not comments:
        return 0

    now = datetime.now(UTC).replace(tzinfo=None)
    expires = now + timedelta(days=RETENTION_DAYS)

    rows = [
        {
            "channel_id": channel_id,
            "youtube_comment_id": c.youtube_comment_id,
            "video_id": c.video_id,
            "parent_comment_id": c.parent_comment_id,
            "is_reply": c.is_reply,
            "author_name": c.author_name,
            "author_channel_id": c.author_channel_id,
            "content": c.content,
            "like_count": c.like_count,
            "total_reply_count": c.total_reply_count,
            "published_at": c.published_at,
            "content_updated_at": c.content_updated_at,
            "collected_at": now,
            # 30일 뒤 원문·작성자 정보 파기 대상 (YouTube API 정책)
            "retention_expires_at": expires,
            "youtube_moderation": c.youtube_moderation,
        }
        for c in comments
    ]

    # 한 번에 다 넣으면 안 된다. PostgreSQL 은 쿼리당 바인드 파라미터가 32,767개로
    # 막혀 있는데, 컬럼이 16개라 2,047행이 상한이다. 논쟁 영상 하나가 그걸 쉽게
    # 넘긴다 (실제로 2,000건 수집에서 터졌다). 넉넉히 나눠 보낸다.
    for i in range(0, len(rows), UPSERT_CHUNK):
        chunk = rows[i : i + UPSERT_CHUNK]
        stmt = insert(Comment).values(chunk)
        # SET 안의 Comment.* 는 고치기 전 값이다. 그래서 순서와 상관없이 옛 본문과 비교된다.
        바뀜 = and_(
            Comment.content.is_distinct_from(stmt.excluded.content),
            Comment.content != MARK,
            Comment.reviewed_at.is_(None),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["youtube_comment_id"],
            set_={
                # 보관기한이 지나 지운 원문은 되살리지 않는다. 보류 목록은 매번 처음부터 다시
                # 받아서, 이게 없으면 30일 지난 보류 댓글의 원문이 다음 파기 때까지 돌아와 있었다.
                "content": case((Comment.content == MARK, Comment.content), else_=stmt.excluded.content),
                "like_count": stmt.excluded.like_count,
                "total_reply_count": stmt.excluded.total_reply_count,
                "content_updated_at": stmt.excluded.content_updated_at,
                # 불변값이지만 갱신 대상에 넣어둔다. 잘못 저장된 행이 있어도
                # 다시 수집하면 스스로 고쳐진다 (video_id 빈 값 사고가 실제로 났다).
                "video_id": stmt.excluded.video_id,
                # API 키로 받은 공개 목록은 이 값을 안 준다(None). None 으로 덮으면
                # 보류였던 표시가 지워지므로, 값이 온 때만 바꾼다.
                "youtube_moderation": func.coalesce(
                    stmt.excluded.youtube_moderation, Comment.youtube_moderation
                ),
                "status": case((바뀜, "pending"), else_=Comment.status),
                # 비슷했던 댓글 검색이 옛 본문으로 찾지 않게 임베딩도 다시 만든다.
                "embedding": case((바뀜, null()), else_=Comment.embedding),
            },
        )
        await db.execute(stmt)
    await db.commit()
    return len(rows)
