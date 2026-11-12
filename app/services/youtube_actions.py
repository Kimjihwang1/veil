"""관리자 조치를 실제 유튜브에 반영한다 (F_R_115).

[숨김] 을 누르면 여기를 거쳐 유튜브 댓글창에서 실제로 가려진다.
실채널로 확인했다 (2026-09-15, 숨김 → 시청자 조회에서 사라짐).

**되돌리기는 안 된다.** setModerationStatus(published) 는 가려진 댓글에 대해
204 를 돌려주면서 아무것도 하지 않는다. heldForReview 를 거쳐도 같다. 유튜브
API 는 rejected 댓글을 읽을 수도 되돌릴 수도 없다 — 실채널로 세 번 확인했다
(2026-09-16). 그래서 '복구' 는 우리 기록을 큐로 돌리는 것이고, 유튜브 쪽은
읽어서 확인된 경우에만 반영됨으로 친다 (is_published).

알아둘 것 두 가지.

  1. **댓글을 지울 수는 없다.** 유튜브가 허용하는 건 '비공개 처리'(rejected)
     뿐이다. 남들에게 안 보이게 되지만 작성자 본인에게는 보일 수 있다.
     그래서 화면에도 '삭제'가 아니라 '숨김'이라고 쓴다.

  2. **채널 소유자 권한이 있어야 한다.** 우리가 수집만 한 채널(진용진 등)은
     조치를 못 한다. 주인이 연동하면서 허락해줘야 토큰이 생긴다.
"""

from dataclasses import dataclass

import httpx

from app.core.config import get_settings
from app.services.google_oauth import refresh_access_token

API = "https://www.googleapis.com/youtube/v3"

# 유튜브가 허용하는 처리 상태. 'deleted' 같은 건 없다.
HIDE = "rejected"       # 비공개 — 남들에게 안 보인다
PUBLISH = "published"   # 공개 — 숨겼던 걸 되돌린다


class NotConnected(RuntimeError):
    """채널 소유자가 연동하지 않아 조치 권한이 없다."""


@dataclass
class ActionResult:
    ok: bool
    detail: str = ""


async def _access_token(refresh_token: str | None) -> str:
    cfg = get_settings()
    if not refresh_token:
        raise NotConnected(
            "이 채널은 소유자 연동이 안 돼 있습니다. "
            "채널 관리에서 [채널 연결]을 먼저 해주세요."
        )
    if not cfg.oauth_ready:
        raise NotConnected(".env 에 GOOGLE_CLIENT_ID / SECRET 이 없습니다.")
    try:
        return await refresh_access_token(
            cfg.google_client_id, cfg.google_client_secret, refresh_token
        )
    except RuntimeError as e:
        # 리프레시 토큰이 죽은 경우. 구글 앱이 '테스트' 상태면 토큰이 7일 뒤 만료된다 —
        # 테스트 채널이 9/15 연결 → 9/22 만료 → 9/29·30 숨김이 전부 조용히 실패했다.
        # '유튜브 오류: RuntimeError' 로 남으면 관리자가 뭘 해야 하는지 모른다.
        if "invalid_grant" in str(e):
            raise NotConnected(
                "유튜브 권한이 만료되었거나 취소됐습니다. 채널 관리에서 [다시 연결]을 "
                "눌러 채널 주인 계정으로 다시 허용해 주세요. (테스트 상태의 구글 앱은 7일마다 만료)"
            ) from e
        raise


# 감시가 보류 댓글을 채널 주인 자격으로 받을 때도 같은 토큰을 쓴다 (watcher).
access_token = _access_token


def _hint(status: int, body: str) -> str:
    """구글 오류를 원인이 보이는 문장으로 바꾼다."""
    낱말 = {
        "insufficientPermissions": (
            "권한이 모자랍니다. 연동할 때 'YouTube 관리' 항목을 허용했는지 "
            "확인하고, 채널을 다시 연결해주세요."
        ),
        "forbidden": "이 채널의 댓글을 관리할 권한이 없습니다. 소유자 계정으로 연동해야 합니다.",
        "commentNotFound": "유튜브에서 이미 사라진 댓글입니다.",
        "processingFailure": "유튜브 쪽 일시 오류입니다. 잠시 뒤 다시 시도해주세요.",
        "invalid_grant": "연동이 해제됐습니다. 채널을 다시 연결해주세요.",
    }
    for k, v in 낱말.items():
        if k in body:
            return v
    return f"유튜브 오류 {status}: {body[:160]}"


async def set_moderation(
    refresh_token: str | None, youtube_comment_ids: list[str], status: str
) -> ActionResult:
    """댓글을 비공개 처리하거나 되돌린다.

    한 번에 여러 개를 보낼 수 있다(쉼표로 이어서). 관리자가 목록에서
    여러 건을 한꺼번에 처리하는 걸 염두에 둔 것이다.
    """
    if not youtube_comment_ids:
        return ActionResult(True, "대상 없음")

    token = await _access_token(refresh_token)
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(
            f"{API}/comments/setModerationStatus",
            params={"id": ",".join(youtube_comment_ids), "moderationStatus": status},
            headers={"Authorization": f"Bearer {token}"},
        )
    # 성공이면 204 No Content 가 온다. 본문이 없다.
    if r.status_code in (200, 204):
        return ActionResult(True)
    return ActionResult(False, _hint(r.status_code, r.text))


async def is_published(refresh_token: str | None, youtube_comment_id: str) -> bool:
    """이 댓글이 지금 유튜브에서 공개 상태인가. 채널 주인 권한으로 읽는다.

    setModerationStatus(published) 의 204 는 믿을 수 없다. 가려진 댓글에는
    204 가 오고도 아무 변화가 없다. 그래서 복구는 호출이 성공했는지가 아니라
    읽어서 실제로 보이는지로 판정한다.

    유튜브 API 는 rejected 댓글을 어떤 방법으로도 돌려주지 않으므로, 여기서
    안 나오면 여전히 가려진 것이다.
    """
    token = await _access_token(refresh_token)
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(
            f"{API}/comments",
            params={"part": "id", "id": youtube_comment_id},
            headers={"Authorization": f"Bearer {token}"},
        )
    return bool(r.is_success and r.json().get("items"))


async def ban_author(
    refresh_token: str | None, youtube_comment_id: str
) -> ActionResult:
    """작성자를 이 채널에서 차단한다.

    플랫폼 전체 차단이 아니다. 이 채널에만 적용된다 — 그래서 화면에도
    '채널 차단'이라고 쓴다. 유튜브 API 는 댓글 ID 로 차단을 건다.
    """
    token = await _access_token(refresh_token)
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(
            f"{API}/comments/setModerationStatus",
            params={
                "id": youtube_comment_id,
                "moderationStatus": HIDE,
                "banAuthor": "true",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
    if r.status_code in (200, 204):
        return ActionResult(True)
    return ActionResult(False, _hint(r.status_code, r.text))


async def my_channels(access_token: str) -> list[dict]:
    """연동한 계정이 소유한 채널 목록.

    보통 하나지만, 브랜드 계정을 여럿 가진 사람도 있다.
    """
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(
            f"{API}/channels",
            params={"part": "snippet", "mine": "true", "maxResults": 50},
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if not r.is_success:
        raise RuntimeError(_hint(r.status_code, r.text))
    return [
        {
            "youtube_channel_id": it["id"],
            "title": it["snippet"].get("title") or "(제목 없음)",
        }
        for it in r.json().get("items", [])
    ]
