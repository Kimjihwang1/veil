"""채널 목록과 채널별 자동 숨김 설정."""

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete, select
from sqlalchemy import text as sq
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.deps import (
    current_user,
    my_workspace_ids,
    optional_user,
    require_channel,
)
from app.db.models import (
    INVITE_DAYS,
    Action,
    Channel,
    ChannelInvite,
    ChannelRule,
    Comment,
    Evidence,
    LabelTask,
    RiskAssessment,
    User,
    Video,
    Workspace,
)
from app.db.session import get_db
from app.services import google_oauth as goog
from app.services import youtube_actions as yt
from app.services.resend import resend_unsynced

log = logging.getLogger("outlier.channels")

router = APIRouter(prefix="/channels", tags=["channels"])

# 자동 숨김을 켤 수 있는 분류. llm.py 의 카테고리에서 '정상'을 뺀 것이다
# (정상을 가린다는 건 말이 안 된다).
#
# 순서는 화면에 그대로 쓰인다. 되돌릴 수 없는 피해가 큰 것부터 놓았다.
HIDEABLE: list[tuple[str, str]] = [
    ("신상털기", "실명·주소·연락처 노출"),
    ("위협", "신체적 위해 암시"),
    ("자해", "자해·자살 관련"),
    ("성희롱", "성적 대상화"),
    ("혐오", "지역·성별·국적 비하"),
    ("욕설", "대상을 겨냥한 욕설"),
    ("괴롭힘", "반복적 시달림"),
    ("모욕", "인신공격·조롱"),
    ("스팸", "홍보·링크 도배"),
    ("기타", "위 어디에도 안 맞는 유해"),
]
HIDEABLE_NAMES = {n for n, _ in HIDEABLE}


class ChannelOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    channel_title: str | None
    connected: bool = Field(
        False,
        description="유튜브 조치 권한(리프레시 토큰)이 있는지. "
        "없으면 숨김을 눌러도 우리 기록에만 남는다.",
    )
    # 누가 언제 연결했는지. 채널 10개면 "이 채널은 누구 열쇠로 돌아가나" 가
    # 헷갈리기 쉬워서 화면에 보여준다. 여기의 '누구' 는 우리 계정(로그인한 사람)이다.
    # 유튜브 권한을 준 구글 계정 자체는 저장하지 않는다.
    connected_at: datetime | None = None
    connected_by: str | None = Field(None, description="연결을 누른 사람의 이메일")


class CategoryOption(BaseModel):
    name: str
    description: str
    enabled: bool


class AutoHideOut(BaseModel):
    channel_id: int
    categories: list[str] = Field(..., description="지금 켜져 있는 분류")
    options: list[CategoryOption] = Field(..., description="켤 수 있는 전체 목록")


class AutoHideUpdate(BaseModel):
    categories: list[str] = Field(
        ..., description="자동 숨김할 분류. 빈 배열이면 자동 숨김 없음"
    )

    @field_validator("categories")
    @classmethod
    def _known(cls, v: list[str]) -> list[str]:
        unknown = [c for c in v if c not in HIDEABLE_NAMES]
        if unknown:
            raise ValueError(f"켤 수 없는 분류: {', '.join(unknown)}")
        # 중복을 없애되 HIDEABLE 순서를 지킨다 (저장값이 화면 순서와 같아진다).
        chosen = set(v)
        return [n for n, _ in HIDEABLE if n in chosen]


CONNECT_PATH = "/api/channels/connect/callback"


def _connect_uri() -> str:
    return get_settings().oauth_redirect_base.rstrip("/") + CONNECT_PATH


@router.get("/connect/start", include_in_schema=False)
async def connect_start(
    user: User | None = Depends(optional_user), db: AsyncSession = Depends(get_db)
):
    """채널 소유자에게 '댓글 관리' 권한을 요청한다.

    로그인과 흐름을 나눈 이유: 로그인은 이메일만 있으면 되는데, 여기서는
    댓글을 가릴 수 있는 권한까지 달라고 해야 한다. 로그인할 때부터 그걸
    요구하면 동의 화면이 과해지고, 채널을 안 붙일 팀원도 있다.
    """
    # 브라우저에서 바로 누르는 버튼이다. 로그인이 안 돼 있으면 401 JSON 이
    # 그대로 보여서 길이 막힌다 — 로그인으로 보내고, 끝나면 여기로 돌아온다.
    if user is None:
        return RedirectResponse("/api/auth/start?next=/api/channels/connect/start")

    cfg = get_settings()
    if not cfg.oauth_ready:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            ".env 에 GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET 이 없습니다.",
        )

    ws = await my_workspace_ids(db, user)
    if not ws:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "워크스페이스가 없습니다")

    state = goog.states.issue(kind="connect", user_id=user.id, workspace_id=ws[0])
    return RedirectResponse(
        goog.authorize_url(
            cfg.google_client_id,
            _connect_uri(),
            goog.LOGIN_SCOPES + goog.YOUTUBE_SCOPES,
            state,
            # 리프레시 토큰이 있어야 나중에도 조치를 할 수 있다.
            # 이게 없으면 1시간 뒤부터 숨김이 안 된다.
            offline=True,
            # 로그인한 계정을 미리 골라둔다. 채널이 같은 계정에 있으면 한 번 클릭.
            login_hint=user.email,
        )
    )


@router.get("/connect/callback", include_in_schema=False)
async def connect_callback(
    state: str = Query(...),
    code: str | None = Query(None),
    error: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    cfg = get_settings()
    data = goog.states.take(state)
    if data is None or data.get("kind") != "connect":
        # 구글 화면에 10분 넘게 머물렀거나 그 사이 서버가 재시작되면 state 가 없다.
        # 전에는 400 JSON 만 떠서 유튜버가 무슨 일인지 몰랐다. 안내 페이지로 보낸다.
        return RedirectResponse("/connect/done?error=expired")

    # 초대 링크로 온 유튜버는 우리 사이트에 로그인이 없다. /app 으로 보내면
    # 로그인 화면이 뜨니, 결과만 보여주는 공개 페이지로 돌려보낸다.
    invite_id = data.get("invite_id")

    def back(q: str) -> str:
        return f"/connect/done?{q}" if invite_id else f"/app#/channels?{q}"

    if error or not code:
        return RedirectResponse(back(f"error={error or 'cancelled'}"))

    # 링크는 한 번만 쓴다. 시작할 때(connect_via_invite) 확인했어도, 같은 링크를 두 탭에서
    # 열면 둘 다 구글 화면으로 넘어간다. 여기서 다시 보지 않으면 둘 다 채널을 붙였다.
    if invite_id:
        inv = await db.get(ChannelInvite, invite_id)
        if inv is None or not inv.usable():
            return RedirectResponse(back("error=invite_invalid"))

    # 구글과의 통신이 실패하면 처리 안 된 500 이 그대로 보였다. 초대 링크로 온 유튜버는
    # 로그인도 없이 서버 오류 화면을 보고 끝난다. 안내 페이지로 보낸다.
    try:
        tokens = await goog.exchange_code(
            cfg.google_client_id, cfg.google_client_secret, code, _connect_uri()
        )
    except Exception:
        log.exception("채널 연결: 구글 토큰 교환 실패")
        return RedirectResponse(back("error=google_failed"))
    if not tokens.refresh_token:
        # prompt=consent 를 줬는데도 안 오면, 이미 허용해둔 상태다.
        # 구글 계정 설정에서 접근을 지우고 다시 해야 한다.
        return RedirectResponse(back("error=no_refresh_token"))

    try:
        owned = await yt.my_channels(tokens.access_token)
    except Exception:
        log.exception("채널 연결: 채널 목록 조회 실패")
        return RedirectResponse(back("error=google_failed"))
    if not owned:
        return RedirectResponse(back("error=no_channel"))

    now = datetime.now(UTC).replace(tzinfo=None)
    붙임, 막힘, 붙은이름, 붙은채널 = 0, [], [], []
    for ch in owned:
        row = (
            await db.execute(
                select(Channel).where(
                    Channel.youtube_channel_id == ch["youtube_channel_id"]
                )
            )
        ).scalar_one_or_none()

        # 이미 다른 워크스페이스가 쓰고 있으면 넘겨받지 않는다.
        #
        # 구글이 소유권을 보증해줬더라도, 그 채널에는 앞사람이 모은 댓글과
        # 판정·조치 이력이 딸려 있다. 그게 통째로 넘어가면 앞사람은 자기
        # 데이터를 잃고, 새 사람은 자기가 모으지 않은 남의 기록을 보게 된다.
        # 어느 쪽도 조용히 일어나면 안 되는 일이라, 막고 알린다.
        if (
            row is not None
            and row.workspace_id is not None
            and row.workspace_id != data["workspace_id"]
        ):
            막힘.append(ch["title"])
            continue

        if row is None:
            row = Channel(youtube_channel_id=ch["youtube_channel_id"])
            db.add(row)
        row.channel_title = ch["title"]
        row.workspace_id = data["workspace_id"]
        row.connected_by_user_id = data["user_id"]
        row.connected_at = now
        row.youtube_refresh_token = tokens.refresh_token
        # 동의는 연동 화면에서 따로 받는다. 여기서는 연동만.
        붙임 += 1
        붙은이름.append(ch["title"])
        붙은채널.append(row)

    if invite_id and 붙임:
        # 링크는 한 번만 쓴다. 누가 뭘 붙였는지 관리자 목록에 남긴다.
        inv = await db.get(ChannelInvite, invite_id)
        if inv is not None:
            inv.used_at = now
            inv.result = ", ".join(붙은이름)[:500]

    await db.commit()
    if 막힘 and not 붙임:
        return RedirectResponse(back("error=already_connected"))

    # 권한이 끊긴 동안 누른 숨김을 지금 유튜브에 보낸다. 다시 연결했는데 댓글이
    # 그대로 보이면 관리자는 연결이 안 된 줄 안다 (2026-11-03 실제로 그랬다).
    보냄 = 반영 = 0
    for row in 붙은채널:
        st = await resend_unsynced(db, row)
        보냄 += st.tried
        반영 += st.synced
    q = f"connected={붙임}" + (f"&resent={보냄}&resent_ok={반영}" if 보냄 else "")
    return RedirectResponse(back(q))


# ── 초대 링크: 관리자가 만들고, 유튜버가 로그인 없이 권한만 준다 ──────────
#
# MCN 관리자는 소속 유튜버의 구글 계정을 모른다. 채널 권한은 유튜버가 구글 화면을
# 직접 통과해야 나온다. 유튜버가 옆에 없을 때 쓰는 길이 이것이다.
# 자세한 이유는 models.ChannelInvite 주석.


class InviteIn(BaseModel):
    note: str | None = Field(None, max_length=100, description="누구에게 보내는 링크인지 메모")


class InviteOut(BaseModel):
    id: int
    url: str
    note: str | None
    created_at: datetime
    expires_at: datetime
    used_at: datetime | None
    result: str | None = Field(None, description="링크로 붙은 채널 이름")


def _invite_url(token: str) -> str:
    return get_settings().oauth_redirect_base.rstrip("/") + f"/connect/{token}"


def _invite_out(i: ChannelInvite) -> InviteOut:
    return InviteOut(
        id=i.id, url=_invite_url(i.token), note=i.note, created_at=i.created_at,
        expires_at=i.expires_at, used_at=i.used_at, result=i.result,
    )


@router.post("/invites", response_model=InviteOut, status_code=201, summary="채널 연결 초대 링크 만들기")
async def create_invite(
    payload: InviteIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    ws = await my_workspace_ids(db, user)
    if not ws:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "워크스페이스가 없습니다")
    inv = ChannelInvite.new(ws[0], user.id, (payload.note or "").strip() or None)
    db.add(inv)
    await db.commit()
    await db.refresh(inv)
    return _invite_out(inv)


@router.get("/invites", response_model=list[InviteOut], summary="내가 만든 초대 링크")
async def list_invites(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    ws = await my_workspace_ids(db, user)
    if not ws:
        return []
    rows = (
        await db.execute(
            select(ChannelInvite)
            .where(ChannelInvite.workspace_id.in_(ws))
            .order_by(ChannelInvite.id.desc())
            .limit(50)
        )
    ).scalars().all()
    return [_invite_out(i) for i in rows]


class InviteInfo(BaseModel):
    """유튜버가 링크를 열었을 때 보는 것. 로그인 없이 준다 — 그래서 최소만."""

    valid: bool
    reason: str | None = None
    inviter_name: str | None = None
    workspace_name: str | None = None
    expires_at: datetime | None = None


async def _load_invite(db: AsyncSession, token: str) -> ChannelInvite | None:
    return (
        await db.execute(select(ChannelInvite).where(ChannelInvite.token == token))
    ).scalar_one_or_none()


@router.get("/invites/{token}/info", response_model=InviteInfo, summary="초대 링크 확인 (공개)")
async def invite_info(token: str, db: AsyncSession = Depends(get_db)):
    inv = await _load_invite(db, token)
    if inv is None:
        return InviteInfo(valid=False, reason="없는 링크입니다")
    if inv.used_at is not None:
        return InviteInfo(valid=False, reason="이미 사용한 링크입니다")
    if not inv.usable():
        return InviteInfo(valid=False, reason=f"기한({INVITE_DAYS}일)이 지난 링크입니다")
    inviter = await db.get(User, inv.created_by_user_id)
    ws = await db.get(Workspace, inv.workspace_id)
    return InviteInfo(
        valid=True,
        inviter_name=(inviter.name or inviter.email) if inviter else None,
        workspace_name=ws.name if ws else None,
        expires_at=inv.expires_at,
    )


@router.get("/connect/invite/{token}", include_in_schema=False)
async def connect_via_invite(token: str, db: AsyncSession = Depends(get_db)):
    """유튜버가 [권한 주기] 를 누르면 여기로 온다. 로그인 없음.

    state 에 초대를 만든 관리자와 워크스페이스를 실어서, 콜백이 채널을 그 워크스페이스에
    붙이게 한다. 콜백 코드는 관리자가 직접 연결할 때와 같다 — 길만 다르다.
    """
    inv = await _load_invite(db, token)
    if inv is None or not inv.usable():
        return RedirectResponse("/connect/done?error=invite_invalid")
    cfg = get_settings()
    if not cfg.oauth_ready:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "구글 OAuth 설정이 없습니다")
    state = goog.states.issue(
        kind="connect", user_id=inv.created_by_user_id, workspace_id=inv.workspace_id,
        invite_id=inv.id,
    )
    return RedirectResponse(
        goog.authorize_url(
            cfg.google_client_id, _connect_uri(),
            goog.LOGIN_SCOPES + goog.YOUTUBE_SCOPES, state, offline=True,
        )
    )


class ConsentOut(BaseModel):
    channel_id: int
    agreed: bool
    agreed_at: datetime | None
    can_judge: bool = Field(
        ..., description="동의 전에는 판별을 돌리지 않는다"
    )


class ConsentUpdate(BaseModel):
    agreed: bool = Field(..., description="AI 자동 판별에 동의하는지")


def _consent_out(channel: Channel) -> ConsentOut:
    ok = bool(channel.ai_consent_agreed)
    return ConsentOut(
        channel_id=channel.id,
        agreed=ok,
        agreed_at=channel.ai_consent_at,
        can_judge=ok,
    )


@router.get(
    "/{channel_id}/consent", response_model=ConsentOut, summary="AI 판별 동의 조회"
)
async def get_consent(channel: Channel = Depends(require_channel)):
    return _consent_out(channel)


@router.put(
    "/{channel_id}/consent", response_model=ConsentOut, summary="AI 판별 동의 변경"
)
async def set_consent(
    payload: ConsentUpdate,
    channel: Channel = Depends(require_channel),
    db: AsyncSession = Depends(get_db),
):
    """AI 자동 판별에 대한 관리자 명시 동의 (YouTube API 정책).

    동의를 철회하면 이후 판별이 멈춘다. 이미 내려진 판정은 지우지 않는다 —
    그건 관리자가 실제로 보고 조치한 근거라, 소급해 없애면 이력이 깨진다.
    지우고 싶으면 연동 해제를 쓴다(그쪽은 전부 지운다).
    """
    channel.ai_consent_agreed = payload.agreed
    channel.ai_consent_at = (
        datetime.now(UTC).replace(tzinfo=None) if payload.agreed else None
    )
    await db.commit()
    await db.refresh(channel)
    return _consent_out(channel)


class DisconnectResult(BaseModel):
    channel_id: int
    deleted_comments: int
    deleted_assessments: int
    deleted_actions: int


@router.post(
    "/{channel_id}/disconnect",
    response_model=DisconnectResult,
    summary="채널 연동 해제 (수집 데이터 전부 삭제)",
)
async def disconnect(
    channel: Channel = Depends(require_channel), db: AsyncSession = Depends(get_db)
):
    """연동을 끊고 그 채널에서 모은 것을 전부 지운다.

    두 가지를 반드시 한다.

      1. **토큰을 지운다.** 남겨두면 해제한 뒤에도 그 채널에 쓰기가 된다.
      2. **수집 데이터를 지운다.** YouTube API 정책상 연동을 끊으면 그 채널
         관련 데이터를 즉시 삭제해야 한다.

    전에는 workspace_id 만 비웠는데, 그러면 댓글이 DB 에 그대로 남은 채
    아무도 못 보는 상태가 된다 — 지운 것도 아니고 쓸 수 있는 것도 아니다.
    """
    cid = channel.id

    # 지우는 순서가 있다. 참조하는 쪽부터 지워야 외래키에 걸리지 않는다.
    comment_ids = select(Comment.id).where(Comment.channel_id == cid).scalar_subquery()

    액션 = await db.execute(
        delete(Action).where(Action.comment_id.in_(comment_ids))
    )
    판정 = await db.execute(
        delete(RiskAssessment).where(RiskAssessment.comment_id.in_(comment_ids))
    )
    # 사람 라벨(label_tasks)과 증거(evidence)도 댓글을 가리킨다. label_tasks 를 안 지우면
    # 외래키에 걸려 연동 해제가 통째로 실패했다 (라벨링에 쓴 채널이면 그렇다, 2026-11-09).
    await db.execute(delete(LabelTask).where(LabelTask.comment_id.in_(comment_ids)))
    await db.execute(delete(Evidence).where(Evidence.channel_id == cid))
    # review_history 는 초안 테이블이라 ORM 모델이 없고, init_db 가 만들지 않아서 DB 에
    # 따라 없을 수 있다. 없는데 DELETE 를 보내면 연동 해제 전체가 실패한다.
    if (await db.execute(sq("SELECT to_regclass('review_history')"))).scalar() is not None:
        await db.execute(
            sq("DELETE FROM review_history WHERE comment_id IN "
               "(SELECT id FROM comments WHERE channel_id = :cid)"),
            {"cid": cid},
        )
    댓글 = await db.execute(delete(Comment).where(Comment.channel_id == cid))
    await db.execute(delete(Video).where(Video.channel_id == cid))
    await db.execute(delete(ChannelRule).where(ChannelRule.channel_id == cid))

    # 채널 행 자체는 남긴다. 다시 연동하면 같은 자리에 붙고, 무엇보다
    # 연동 이력이 있었다는 사실은 남아야 한다.
    channel.youtube_refresh_token = None
    channel.workspace_id = None
    channel.context = None
    channel.auto_hide_categories = None
    channel.ai_consent_agreed = None
    await db.commit()

    return DisconnectResult(
        channel_id=cid,
        deleted_comments=댓글.rowcount or 0,
        deleted_assessments=판정.rowcount or 0,
        deleted_actions=액션.rowcount or 0,
    )


class ContextOut(BaseModel):
    channel_id: int
    context: str = Field("", description="이 채널의 판단 기준")
    prompt_version: str = Field(..., description="지금 이 기준으로 매기면 남는 지문")
    updated_hint: str = Field(
        ..., description="바꾼 뒤 무엇을 해야 반영되는지"
    )


class ContextUpdate(BaseModel):
    context: str = Field(
        "", max_length=4000,
        description="영상이 바뀌어도 그대로인 기준만. 영상별 설명은 영상에 적는다",
    )


def _context_out(channel: Channel) -> ContextOut:
    from app.services.llm import prompt_version

    ctx = channel.context or ""
    return ContextOut(
        channel_id=channel.id,
        context=ctx,
        prompt_version=prompt_version(ctx, channel.prompt_template),
        updated_hint=(
            "바꾼 기준은 다음 판별부터 적용됩니다. 이미 판별한 댓글에 적용하려면 "
            f"scripts/rejudge.py --channel {channel.id} --all 을 돌립니다."
        ),
    )


@router.get(
    "/{channel_id}/context", response_model=ContextOut, summary="채널 기준 조회"
)
async def get_context(channel: Channel = Depends(require_channel)):
    return _context_out(channel)


@router.put(
    "/{channel_id}/context", response_model=ContextOut, summary="채널 기준 변경"
)
async def set_context(
    payload: ContextUpdate,
    channel: Channel = Depends(require_channel),
    db: AsyncSession = Depends(get_db),
):
    """이 채널에서만 통하는 판단 기준을 적는다.

    프롬프트 맨 뒤에 붙는다 — 앞부분(모든 채널 공통)이 고정이어야 캐싱이
    걸려서 값이 1/10 이 된다.

    이미 판별한 댓글에는 소급하지 않는다. 기준을 고칠 때마다 수천 건이
    자동으로 다시 돌면 돈이 얼마나 나갈지 예측할 수 없기 때문이다.
    """
    channel.context = payload.context.strip() or None
    await db.commit()
    await db.refresh(channel)
    return _context_out(channel)


@router.get("", response_model=list[ChannelOut], summary="채널 목록")
async def list_channels(
    user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    """내가 속한 워크스페이스의 채널만. 채널간 노출은 정책상 금지다."""
    ws = await my_workspace_ids(db, user)
    if not ws:
        return []
    result = await db.execute(
        select(Channel, User.email)
        .outerjoin(User, User.id == Channel.connected_by_user_id)
        .where(Channel.workspace_id.in_(ws))
        .order_by(Channel.id)
    )
    # 토큰 자체는 절대 내보내지 않는다. 있는지 여부만 알려준다 —
    # 화면이 '조치가 유튜브에 반영되는 채널'인지 구분할 수 있어야 해서다.
    return [
        ChannelOut(
            id=c.id,
            channel_title=c.channel_title,
            connected=bool(c.youtube_refresh_token),
            connected_at=c.connected_at if c.youtube_refresh_token else None,
            connected_by=email if c.youtube_refresh_token else None,
        )
        for c, email in result.all()
    ]


def _out(channel: Channel) -> AutoHideOut:
    on = channel.auto_hide_set
    return AutoHideOut(
        channel_id=channel.id,
        categories=[n for n, _ in HIDEABLE if n in on],
        options=[
            CategoryOption(name=n, description=d, enabled=n in on) for n, d in HIDEABLE
        ],
    )


@router.get(
    "/{channel_id}/auto-hide",
    response_model=AutoHideOut,
    summary="자동 숨김 설정 조회",
)
async def get_auto_hide(channel: Channel = Depends(require_channel)):
    return _out(channel)


@router.put(
    "/{channel_id}/auto-hide",
    response_model=AutoHideOut,
    summary="자동 숨김 설정 변경",
)
async def set_auto_hide(
    payload: AutoHideUpdate,
    channel: Channel = Depends(require_channel),
    db: AsyncSession = Depends(get_db),
):
    """켠 분류는 이후 판별부터 사람을 안 거치고 가려진다.

    이미 판별된 댓글에는 소급되지 않는다 — 바꾼 정책을 과거 것에 적용하려면
    scripts/reroute.py 를 돌린다. 화면에서 누르자마자 예전 댓글이 무더기로
    사라지는 편이 더 위험해서 이렇게 나눴다.
    """
    channel.auto_hide_categories = ",".join(payload.categories) or None
    await db.commit()
    await db.refresh(channel)
    return _out(channel)
