"""수집기 테스트.

httpx.MockTransport 로 유튜브 응답을 흉내낸다. 실제 API를 부르지 않으므로
쿼터도 안 쓰고 네트워크 없이 돌아간다. 대신 요청 경로/파라미터는 실제 코드
경로를 그대로 타므로, 페이지네이션과 답글 보충 로직이 진짜로 검증된다.
"""

import httpx
import pytest

from app.services.collector import (
    YouTubeApiError,
    YouTubeCollector,
    extract_video_id,
)


def _snippet(text: str, **extra) -> dict:
    return {
        "textOriginal": text,
        "textDisplay": f"<b>{text}</b>",  # 이걸 쓰면 안 된다 (태그 오탐)
        "authorDisplayName": "tester",
        "authorChannelId": {"value": "UC_author"},
        "publishedAt": "2026-09-01T10:00:00Z",
        "updatedAt": "2026-09-01T10:00:00Z",
        "likeCount": 3,
        "videoId": "vid00000001",
        **extra,
    }


def _thread(tid: str, *, total_replies: int = 0, inline: int = 0) -> dict:
    thread = {
        "id": tid,
        "snippet": {
            "totalReplyCount": total_replies,
            "topLevelComment": {"id": tid, "snippet": _snippet(f"top-{tid}")},
        },
    }
    if inline:
        thread["replies"] = {
            "comments": [
                {"id": f"{tid}.r{i}", "snippet": _snippet(f"reply-{tid}-{i}")}
                for i in range(inline)
            ]
        }
    return thread


def make_client(routes) -> tuple[httpx.AsyncClient, list[str]]:
    """routes: (path 끝부분, 응답 목록) 딕셔너리. 호출 순서대로 하나씩 돌려준다."""
    calls: list[str] = []
    cursor = {k: 0 for k in routes}

    def handler(request: httpx.Request) -> httpx.Response:
        for key in routes:
            if request.url.path.endswith(key):
                calls.append(key)
                i = cursor[key]
                cursor[key] = min(i + 1, len(routes[key]) - 1)
                body = routes[key][i]
                if isinstance(body, int):  # 에러 코드로 쓴다
                    return httpx.Response(
                        body,
                        json={"error": {"message": "nope", "errors": [{"reason": "commentsDisabled"}]}},
                    )
                return httpx.Response(200, json=body)
        raise AssertionError(f"예상 못한 요청: {request.url}")

    client = httpx.AsyncClient(
        base_url="https://www.googleapis.com/youtube/v3",
        transport=httpx.MockTransport(handler),
    )
    return client, calls


async def test_pagination_follows_next_page_token():
    """nextPageToken 이 있으면 끝까지 따라가야 한다."""
    client, calls = make_client(
        {
            "commentThreads": [
                {"items": [_thread("a")], "nextPageToken": "PAGE2"},
                {"items": [_thread("b")]},
            ]
        }
    )
    got = await YouTubeCollector("k", client).collect("vid00000001", orders=("time",))

    assert calls.count("commentThreads") == 2
    assert {c.youtube_comment_id for c in got} == {"a", "b"}


async def test_inline_replies_are_used_without_extra_call():
    """답글이 안 잘렸으면 comments.list 를 부르지 않는다 (쿼터 낭비 금지)."""
    client, calls = make_client(
        {"commentThreads": [{"items": [_thread("a", total_replies=3, inline=3)]}]}
    )
    collector = YouTubeCollector("k", client)
    got = await collector.collect("vid00000001", orders=("time",))

    assert "comments" not in calls
    assert collector.stats.replies_inline == 3
    assert collector.stats.truncated_threads == 0
    assert len(got) == 4  # 최상위 1 + 답글 3


async def test_truncated_thread_is_supplemented():
    """스레드당 5개 상한에 잘린 답글을 comments.list 로 채워야 한다.

    이게 이 수집기를 만든 이유다 — 답글이 많은(=논쟁이 붙은) 스레드일수록
    많이 잘리는데, 거기가 악플이 있을 확률이 가장 높은 곳이다.
    """
    client, calls = make_client(
        {
            "commentThreads": [{"items": [_thread("a", total_replies=7, inline=5)]}],
            "comments": [
                {
                    "items": [
                        {"id": f"a.full{i}", "snippet": _snippet(f"r{i}")} for i in range(7)
                    ]
                }
            ],
        }
    )
    collector = YouTubeCollector("k", client)
    got = await collector.collect("vid00000001", orders=("time",))

    assert calls.count("comments") == 1
    assert collector.stats.truncated_threads == 1
    assert collector.stats.replies_supplemented == 7
    assert len(got) == 8  # 최상위 1 + 답글 7 (5개가 아니라)


async def test_replies_carry_parent_id():
    """답글에는 부모 ID가 붙어야 한다. 3차 LLM 맥락 주입에 필요하다."""
    client, _ = make_client(
        {"commentThreads": [{"items": [_thread("a", total_replies=2, inline=2)]}]}
    )
    got = await YouTubeCollector("k", client).collect("vid00000001")

    top = [c for c in got if not c.is_reply]
    replies = [c for c in got if c.is_reply]

    assert len(top) == 1 and top[0].total_reply_count == 2
    assert len(replies) == 2
    assert all(r.parent_comment_id == "a" for r in replies)


async def test_supplemented_replies_get_video_id():
    """comments.list 응답 snippet 에는 videoId 가 없다 (실제 API 동작).

    수집기가 채워주지 않으면 보충한 답글이 video_id 빈 값으로 저장돼서
    영상별 조회에 안 잡힌다 — 실제로 났던 사고다.
    """
    reply_snippet = _snippet("보충된 답글")
    del reply_snippet["videoId"]

    client, _ = make_client(
        {
            "commentThreads": [{"items": [_thread("a", total_replies=2, inline=0)]}],
            "comments": [{"items": [{"id": "a.r0", "snippet": reply_snippet}]}],
        }
    )
    got = await YouTubeCollector("k", client).collect("vid00000001")

    assert len(got) == 2
    assert all(c.video_id == "vid00000001" for c in got)


async def test_uses_text_original_not_text_display():
    """textDisplay 는 HTML이 섞여 있어 정규식 매칭에 쓰면 오탐이 난다."""
    client, _ = make_client({"commentThreads": [{"items": [_thread("a")]}]})
    got = await YouTubeCollector("k", client).collect("vid00000001")

    assert got[0].content == "top-a"
    assert "<b>" not in got[0].content


async def test_duplicate_ids_are_collapsed():
    """보충 응답에 기본 응답과 겹치는 답글이 있어도 한 건으로 합쳐야 한다."""
    client, _ = make_client(
        {
            "commentThreads": [
                {"items": [_thread("a", total_replies=2, inline=1)]},
            ],
            "comments": [
                {
                    "items": [
                        {"id": "a.r0", "snippet": _snippet("dup")},
                        {"id": "a.r1", "snippet": _snippet("new")},
                    ]
                }
            ],
        }
    )
    got = await YouTubeCollector("k", client).collect("vid00000001")

    ids = [c.youtube_comment_id for c in got]
    assert len(ids) == len(set(ids))


async def test_comments_disabled_returns_empty_not_error():
    """댓글 꺼진 영상에서 죽으면 채널 단위 수집이 통째로 멈춘다."""
    client, _ = make_client({"commentThreads": [403]})
    collector = YouTubeCollector("k", client)

    assert await collector.collect("vid00000001") == []
    assert collector.stats.comments_disabled is True


async def test_other_api_errors_still_raise():
    """쿼터 초과 같은 건 조용히 넘기면 안 된다."""

    def handler(request):
        return httpx.Response(
            403, json={"error": {"message": "quota", "errors": [{"reason": "quotaExceeded"}]}}
        )

    client = httpx.AsyncClient(
        base_url="https://www.googleapis.com/youtube/v3",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(YouTubeApiError) as e:
        await YouTubeCollector("k", client).collect("vid00000001")
    assert e.value.reason == "quotaExceeded"


@pytest.mark.parametrize(
    "raw",
    [
        "https://www.youtube.com/watch?v=35-9KG8v1M8",
        "https://youtu.be/35-9KG8v1M8",
        "https://www.youtube.com/shorts/35-9KG8v1M8",
        "35-9KG8v1M8",
    ],
)
def test_extract_video_id(raw):
    assert extract_video_id(raw) == "35-9KG8v1M8"


def test_extract_video_id_rejects_garbage():
    with pytest.raises(ValueError):
        extract_video_id("https://example.com/hello")


async def test_two_orders_are_merged_and_deduped():
    """인기순·최신순을 따로 훑어 합친다. 겹치는 댓글은 하나로 남아야 한다.

    한 정렬로만 받으면 표본이 치우친다 — 상한에 걸린 time 은 뒤늦게 달린
    댓글만, relevance 는 좋아요 많은 정제된 여론만 남는다. 논란 영상에서
    이 둘은 사실상 다른 집단이라 둘 다 받아야 한다.
    """
    seen_orders: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        order = request.url.params.get("order")
        seen_orders.append(order)
        # a 는 양쪽에 다 나온다. 정렬별로 새로운 것도 하나씩 있다.
        tid = "t_time" if order == "time" else "t_rel"
        return httpx.Response(200, json={"items": [_thread("a"), _thread(tid)]})

    client = httpx.AsyncClient(
        base_url="https://www.googleapis.com/youtube/v3",
        transport=httpx.MockTransport(handler),
    )
    got = await YouTubeCollector("k", client).collect("vid00000001")

    assert seen_orders == ["time", "relevance"]
    assert {c.youtube_comment_id for c in got} == {"a", "t_time", "t_rel"}


async def test_stops_at_page_with_already_stored_thread():
    """최신순으로 받다가 이미 저장한 스레드가 나온 페이지에서 멈춘다.

    감시가 매 주기 '지난번에 본 곳까지' 만 받는 방식이다. 그 앞에 몇 개가
    달렸든 전부 받고, 이미 본 뒤쪽은 다시 받지 않는다 (쿼터 절약).
    """
    client, calls = make_client(
        {
            "commentThreads": [
                {"items": [_thread("new1"), _thread("new2")], "nextPageToken": "P2"},
                {"items": [_thread("new3"), _thread("old1")], "nextPageToken": "P3"},
                {"items": [_thread("old2")]},
            ]
        }
    )
    got = await YouTubeCollector("k", client).collect(
        "vid00000001", orders=("time",), stop_at={"old1", "old2"}
    )

    assert calls.count("commentThreads") == 2      # 세 번째 페이지는 안 부른다
    assert {"new1", "new2", "new3"} <= {c.youtube_comment_id for c in got}


async def test_collect_held_asks_as_channel_owner():
    """보류·스팸 의심 댓글은 채널 주인 자격으로만 받을 수 있다.

    API 키로 부르면 moderationStatus 가 거부된다. 토큰을 헤더에 싣고 키는 안 보내야 하고,
    상태 두 가지를 따로 물어야 한다. 댓글마다 붙어 온 상태값이 남아야 피드에서 구분된다.
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        status = request.url.params["moderationStatus"]
        top = {"id": f"t-{status}", "snippet": _snippet("보류된 댓글", moderationStatus=status)}
        return httpx.Response(200, json={"items": [
            {"id": f"t-{status}", "snippet": {"totalReplyCount": 0, "topLevelComment": top}}
        ]})

    client = httpx.AsyncClient(
        base_url="https://www.googleapis.com/youtube/v3",
        transport=httpx.MockTransport(handler),
    )
    got = await YouTubeCollector("k", client, access_token="tok").collect_held("vid00000001")

    assert [r.url.params["moderationStatus"] for r in seen] == ["heldForReview", "likelySpam"]
    assert all(r.headers["Authorization"] == "Bearer tok" for r in seen)
    assert all("key" not in r.url.params for r in seen)
    assert {c.youtube_comment_id: c.youtube_moderation for c in got} == {
        "t-heldForReview": "heldForReview", "t-likelySpam": "likelySpam",
    }
    assert all(c.video_id == "vid00000001" for c in got)


async def test_collect_held_needs_owner_token():
    with pytest.raises(RuntimeError):
        await YouTubeCollector("k").collect_held("vid00000001")


async def test_public_collection_has_no_moderation_status():
    """API 키로 받은 공개 목록에는 상태값이 없다. None 이어야 저장 때 보류 표시를 안 덮는다."""
    client, _ = make_client({"commentThreads": [{"items": [_thread("p1")]}]})
    got = await YouTubeCollector("k", client).collect("vid00000001", orders=("time",))
    assert [c.youtube_moderation for c in got] == [None]
