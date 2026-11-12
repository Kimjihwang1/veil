"""Google OAuth 2.0 — 라이브러리 없이 httpx 로 직접 한다.

흐름은 세 단계뿐이라 authlib 같은 걸 붙일 이유가 없다.
  ① 구글로 보낸다 (동의 화면)
  ② 구글이 code 를 붙여 돌려보낸다
  ③ code 를 토큰으로 바꾼다

state 를 반드시 검증한다. 안 하면 남이 만든 로그인 링크를 눌렀을 때
그 사람 계정으로 로그인되거나(CSRF), 남의 채널이 내 워크스페이스에 붙는다.
"""

import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

# 로그인에 필요한 최소한.
LOGIN_SCOPES = ["openid", "email", "profile"]

# 댓글을 실제로 가리려면(comments.setModerationStatus) 이게 필요하다.
# readonly 로는 조치를 못 한다.
YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]

# state 유효 시간. 동의 화면에서 오래 머무를 수 있으니 넉넉히 준다.
STATE_TTL = 600


@dataclass
class GoogleTokens:
    access_token: str
    refresh_token: str | None
    expires_in: int


@dataclass
class GoogleProfile:
    sub: str
    email: str
    name: str | None
    picture: str | None


class StateStore:
    """OAuth state 를 잠깐 들고 있는다.

    서버가 한 대라 메모리로 충분하다. 여러 대가 되면 DB 나 Redis 로 옮겨야
    하는데, 그때 여기만 바꾸면 되도록 따로 뺐다.
    """

    def __init__(self) -> None:
        self._items: dict[str, tuple[float, dict]] = {}

    def issue(self, **data) -> str:
        self._sweep()
        state = secrets.token_urlsafe(24)
        self._items[state] = (time.time() + STATE_TTL, data)
        return state

    def take(self, state: str) -> dict | None:
        """한 번만 쓸 수 있다. 재사용하면 재생 공격이 된다."""
        self._sweep()
        item = self._items.pop(state, None)
        return None if item is None else item[1]

    def _sweep(self) -> None:
        now = time.time()
        for k in [k for k, (exp, _) in self._items.items() if exp < now]:
            self._items.pop(k, None)


states = StateStore()


def authorize_url(
    client_id: str,
    redirect_uri: str,
    scopes: list[str],
    state: str,
    *,
    offline: bool = False,
    login_hint: str | None = None,
) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "state": state,
        "include_granted_scopes": "true",
    }
    if login_hint:
        # 채널 연결 때 로그인한 계정을 구글에 미리 알려준다. 같은 계정이면
        # 계정 선택 화면에 그 계정이 골라져 있어 한 번 클릭으로 끝난다.
        # 로그인 → 채널 연결이 "구글 두 번" 으로 보이는 걸 줄이는 장치다.
        # 다른 계정(채널 주인)을 골라야 하면 그냥 바꿔 고르면 된다 — 강제가 아니다.
        params["login_hint"] = login_hint
    if offline:
        # 리프레시 토큰은 offline + consent 를 같이 줘야 확실히 온다.
        # prompt 를 빼면 두 번째 연동부터 refresh_token 이 안 와서,
        # 재연동한 채널만 조용히 조치가 안 되는 일이 생긴다.
        params["access_type"] = "offline"
        params["prompt"] = "consent"
    else:
        # 로그인은 계정을 고르게 한다. 이걸 안 주면, 브라우저에 이미 구글
        # 계정이 붙어 있고 전에 동의까지 했을 때 구글이 아무것도 안 묻고
        # 곧바로 돌려보낸다 — 화면이 번쩍하고 지나가서 로그인이 안 된 걸로
        # 보인다. 계정이 여러 개일 때 어느 쪽으로 들어갔는지도 알 수 없다.
        params["prompt"] = "select_account"
    return f"{AUTH_URL}?{urlencode(params)}"


async def exchange_code(
    client_id: str, client_secret: str, code: str, redirect_uri: str
) -> GoogleTokens:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(
            TOKEN_URL,
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
    _raise(r)
    d = r.json()
    return GoogleTokens(
        access_token=d["access_token"],
        refresh_token=d.get("refresh_token"),
        expires_in=int(d.get("expires_in", 3600)),
    )


async def refresh_access_token(
    client_id: str, client_secret: str, refresh_token: str
) -> str:
    """저장해둔 리프레시 토큰으로 액세스 토큰을 새로 받는다."""
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(
            TOKEN_URL,
            data={
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "refresh_token",
            },
        )
    _raise(r)
    return r.json()["access_token"]


async def fetch_profile(access_token: str) -> GoogleProfile:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(
            USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}
        )
    _raise(r)
    d = r.json()
    return GoogleProfile(
        sub=d["sub"], email=d["email"], name=d.get("name"), picture=d.get("picture")
    )


def _raise(r: httpx.Response) -> None:
    """구글 오류를 원인이 보이는 형태로 올린다.

    그냥 raise_for_status 를 쓰면 본문이 안 보여서, 흔한 설정 실수
    (redirect_uri 불일치, 테스트 사용자 미등록)를 구분할 수 없다.
    """
    if r.is_success:
        return
    try:
        d = r.json()
        detail = f"{d.get('error')}: {d.get('error_description', '')}".strip(": ")
    except Exception:
        detail = r.text[:200]
    hint = {
        "redirect_uri_mismatch": (
            "콘솔의 '승인된 리디렉션 URI' 와 값이 정확히 같아야 한다 "
            "(http/https, 포트, 끝 슬래시까지)."
        ),
        "access_denied": (
            "동의 화면에서 취소했거나, 미검증 앱의 '테스트 사용자'에 "
            "이 계정이 등록돼 있지 않다."
        ),
        "invalid_client": "클라이언트 ID 또는 시크릿이 틀렸다.",
        "invalid_grant": "code 가 이미 쓰였거나 만료됐다. 처음부터 다시 시도한다.",
    }
    key = detail.split(":")[0].strip()
    raise RuntimeError(f"[Google OAuth] {detail}" + (f"\n  >>> {hint[key]}" if key in hint else ""))
