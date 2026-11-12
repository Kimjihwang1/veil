"""ORM 모델."""

import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from pgvector.sqlalchemy import Vector

from app.db.base import Base

# 임베딩 차원. text-embedding-3-small 의 기본값이다.
EMBEDDING_DIM = 1536

# 로그인 세션 유지 기간.
SESSION_DAYS = 14


class User(Base):
    """우리 사이트 계정. 이메일+비밀번호로 가입하거나 Google 로 들어온다.

    처음엔 Google 만 받았다. 유튜브 채널을 붙이려면 어차피 구글 인증이 필요해서다.
    그런데 "우리 사이트 로그인" 과 "채널 주인의 유튜브 권한" 이 둘 다 구글 화면이라
    사용자에게 같은 일을 두 번 하는 것으로 보였다 (2026-09-17 팀 결정). 그래서
    사이트 로그인은 이메일+비밀번호를 기본으로 두고 Google 은 선택지로 남긴다.
    채널 연결은 여전히 채널 주인의 구글 계정으로만 된다 — 그건 유튜브 쪽 일이다.

    이메일 가입과 Google 은 서로 이어붙이지 않는다. 가입 때 이메일 소유를 확인하지
    않아서(메일 인증 없음), 어느 방향으로 합치든 남의 이메일로 먼저 가입해 둔 사람이
    계정을 가져갈 길이 생긴다. Google 계정 이메일로 비밀번호 가입은 409, 비밀번호
    계정 이메일로 Google 로그인은 login_error=password_account. 메일 인증을 붙이면 풀 수 있다.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    name: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # 이메일 가입 계정만 값이 있다. 원문이 아니라 scrypt 해시 (services/password.py).
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 구글이 주는 고유 식별자(sub). 이메일은 바뀔 수 있어서 이걸 기준으로 찾는다.
    google_id: Mapped[str | None] = mapped_column(String(100), unique=True, nullable=True)
    picture: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now())
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Workspace(Base):
    """채널을 담는 단위. 권한과 데이터 격리의 경계다.

    채널을 유저에 직접 매달지 않은 이유는, 한 채널을 여러 관리자가 같이
    봐야 하기 때문이다. 담당자가 바뀌어도 채널 연동이 유지돼야 한다.
    """

    __tablename__ = "workspaces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    type: Mapped[str] = mapped_column(String(20), default="personal")  # personal / team
    created_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now())


class WorkspaceMember(Base):
    """누가 어느 워크스페이스에 속하는지. 이 표에 없으면 접근이 막힌다."""

    __tablename__ = "workspace_members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    role: Mapped[str] = mapped_column(String(20), default="member")  # owner / admin / member
    created_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now())


class Session(Base):
    """로그인 세션.

    JWT 가 아니라 DB 에 두는 이유: 계약 종료·연동 해제 시 '즉시' 끊을 수
    있어야 한다. 서명 토큰은 만료 전까지 회수할 방법이 없다.
    """

    __tablename__ = "sessions"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    @staticmethod
    def new(user_id: int, days: int = SESSION_DAYS) -> "Session":
        now = datetime.now(UTC).replace(tzinfo=None)
        return Session(
            # 32바이트 난수. 추측으로 남의 세션을 맞힐 수 없어야 한다.
            token=secrets.token_urlsafe(32)[:64],
            user_id=user_id,
            created_at=now,
            expires_at=now + timedelta(days=days),
            last_seen_at=now,
        )


class Channel(Base):
    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int | None] = mapped_column(
        ForeignKey("workspaces.id"), nullable=True, index=True
    )
    youtube_channel_id: Mapped[str] = mapped_column(String(64))
    channel_title: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # AI 자동 모더레이션에 대한 관리자 명시 동의 (YouTube API 정책).
    # 동의 없이는 판별을 돌리지 않는다.
    ai_consent_agreed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    ai_consent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    connected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    connected_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )

    # 채널 소유자의 OAuth 리프레시 토큰. 이게 있어야 댓글을 실제로 가릴 수
    # 있다(comments.setModerationStatus). 액세스 토큰은 1시간짜리라 저장하지
    # 않고 필요할 때마다 새로 받는다.
    #
    # 연동을 해제하면 반드시 NULL 로 지운다 — 남겨두면 해제한 뒤에도
    # 그 채널에 쓰기가 가능한 상태가 된다.
    youtube_refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 이 채널에서만 통하는 판단 기준. 프롬프트 맨 뒤에 붙는다 — 앞부분이
    # 고정이어야 캐싱이 걸리기 때문.
    #
    # 지금은 사람이 쓴다. 관리자 조치 이력을 그대로 프롬프트에 넣어 대신하게
    # 하는 건 재봤는데 안 됐다 (사례 5건 → 6/9, 사람이 쓴 규칙 → 9/9, 둘을
    # 합치면 도로 6/9). 사례에서 규칙을 뽑는 단계가 따로 있어야 하고, 그건
    # 아직 없다.
    context: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 이 채널이 사람을 안 거치고 바로 가리기로 정한 분류. 콤마로 구분한다.
    # NULL 또는 빈 문자열이면 자동 숨김 없음 — 새 채널의 기본값이다.
    # 무엇을 자동으로 가릴지는 채널마다 답이 다르다. '모욕'을 어디까지로
    # 볼지는 뉴스 채널과 게임 채널이 같을 수 없어서, 값을 코드가 아니라
    # 채널이 들고 있게 했다.
    auto_hide_categories: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 'AI 프롬프트' 화면에서 관리자가 고쳐 저장한 판별 프롬프트. NULL 이면 기본(llm.py).
    # {채널 기준} 자리에 위의 context 가 들어간다.
    prompt_template: Mapped[str | None] = mapped_column(Text, nullable=True)

    # '자동 감지' 화면. 채널마다 켜고 끄고 주기를 정한다. 주기가 NULL 이면 서버 기본값
    # (WATCH_INTERVAL). last_watched_at 은 서버가 이 채널을 마지막으로 돈 시각이다.
    watch_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    watch_interval_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_watched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    rules: Mapped[list["ChannelRule"]] = relationship(back_populates="channel")

    @property
    def auto_hide_set(self) -> set[str]:
        """auto_hide_categories 를 집합으로. 미설정이면 빈 집합."""
        raw = self.auto_hide_categories or ""
        return {c.strip() for c in raw.split(",") if c.strip()}


class ChannelRule(Base):
    """채널별로 관리자가 등록한 단어 규칙 (F_R_108)."""

    __tablename__ = "channel_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)

    rule_type: Mapped[str] = mapped_column(String(20), default="keyword")

    # 관리자가 입력한 원문. 화면에 보여주고, 수정할 때 쓰는 값.
    rule_value: Mapped[str] = mapped_column(String(255))

    # rule_value로부터 자동 생성한 정규식. 매칭 엔진이 쓰는 값.
    # 등록/수정 시 한 번만 만들어 저장한다 (댓글마다 다시 만들지 않으려고).
    compiled_regex: Mapped[str] = mapped_column(Text)

    # block=즉시 차단 / review=보류 표시 / allow=예외(통과)
    action: Mapped[str] = mapped_column(String(10), default="block")

    # 초성·공백·특수문자 변형까지 잡을지. 고유명사는 꺼두는 게 안전하다.
    expand_variants: Mapped[bool] = mapped_column(Boolean, default=True)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    channel: Mapped["Channel"] = relationship(back_populates="rules")


INVITE_DAYS = 7


class ChannelInvite(Base):
    """채널 연결 초대 링크 — 관리자가 만들어 유튜버에게 보내는 것.

    MCN 관리자는 소속 유튜버의 구글 계정을 모른다 (알아서도 안 된다). 채널 권한은
    유튜버 본인이 구글 화면을 통과해야 나오는데, 지금까지는 관리자가 로그인한
    브라우저 앞에 유튜버가 앉아 있어야 했다. 이 링크가 있으면 유튜버는 우리 사이트에
    가입도 로그인도 없이 링크 하나 열어 권한만 눌러주고, 채널은 관리자 워크스페이스에 붙는다.

    OAuth state(메모리, 10분)로는 안 되는 이유: 카톡으로 보낸 링크는 며칠 뒤에 열리고,
    그 사이 서버가 재시작될 수 있다. 그래서 DB 에 둔다. 한 번 쓰면 끝(used_at).
    """

    __tablename__ = "channel_invites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token: Mapped[str] = mapped_column(String(64), unique=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), index=True)
    created_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    # 누구에게 보내는 링크인지 관리자가 적어두는 메모. 목록에서 구분하는 용도.
    note: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(UTC).replace(tzinfo=None))
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 링크로 붙은 채널 이름들. 기록용.
    result: Mapped[str | None] = mapped_column(String(500), nullable=True)

    @classmethod
    def new(cls, workspace_id: int, user_id: int, note: str | None) -> "ChannelInvite":
        now = datetime.now(UTC).replace(tzinfo=None)
        return cls(
            token=secrets.token_urlsafe(32),
            workspace_id=workspace_id,
            created_by_user_id=user_id,
            note=note,
            created_at=now,
            expires_at=now + timedelta(days=INVITE_DAYS),
        )

    def usable(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(UTC).replace(tzinfo=None)
        return self.used_at is None and self.expires_at > now


class Video(Base):
    """수집 대상 영상.

    topic 은 유튜브가 주는 값이 아니라 우리가 붙이는 라벨이다(정치·범죄·연예…).
    채널이 아니라 '사건' 단위로 데이터를 모으면 채널당 댓글이 흩어져서
    채널별 패턴이 안 보인다. 그래서 topic 을 채널 대신 묶는 축으로 쓴다.
    기획서의 업종별 규칙(F_R_127)에서 '업종' 자리에 들어가는 값이기도 하다.
    """

    __tablename__ = "videos"

    video_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    title: Mapped[str | None] = mapped_column(String(500), nullable=True)

    topic: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)

    # 이 영상에만 해당하는 설명. 판별할 때 프롬프트에 같이 넣는다.
    #
    # 채널 맥락과 나눈 이유: 한 채널이 여러 성격의 영상을 올린다. 진용진은
    # 머니게임도 올리고 길거리 실험도 올린다. "남녀가 팀으로 갈려 싸운다"를
    # 채널에 박아두면 실험 영상에는 틀린 정보가 들어간다.
    #
    # 대개는 비워둬도 된다 — title 만으로도 무슨 영상인지 전달된다.
    # 머니게임처럼 출연자 구도를 알아야 판단이 되는 영상에만 적는다.
    context: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 유튜브가 알려주는 총 댓글 수. 우리가 받은 건수와 비교해야
    # "이 표본이 전체의 몇 %인가"에 답할 수 있다.
    comment_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    collected_at: Mapped[datetime | None] = mapped_column(
        DateTime, server_default=func.now()
    )


class Comment(Base):
    """수집된 댓글 (F_R_107)."""

    __tablename__ = "comments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)

    # 재수집해도 중복이 안 생기도록 unique. upsert의 충돌 기준 컬럼이다.
    youtube_comment_id: Mapped[str] = mapped_column(String(64), unique=True)
    video_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # 답글이면 부모 댓글 ID. 3차 LLM에 맥락으로 넣는다 —
    # "그만해라" 같은 댓글은 부모를 봐야 사람도 판단할 수 있다.
    parent_comment_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    is_reply: Mapped[bool] = mapped_column(Boolean, default=False)

    author_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 반복 악플러 추적 · 채널차단(banAuthor) 대상 식별에 쓴다.
    author_channel_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    content: Mapped[str] = mapped_column(Text)

    # 우선순위 신호. 확산도(좋아요)와 논쟁 강도(답글 수) — 둘 다 수집 시 공짜로 온다.
    like_count: Mapped[int] = mapped_column(Integer, default=0)
    total_reply_count: Mapped[int] = mapped_column(Integer, default=0)

    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 유튜브상 수정 시각. published_at 과 다르면 수정된 댓글 → 재판별 대상.
    content_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    collected_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now())

    # 원문·작성자 정보 파기 기한. 이후엔 파생 지표만 남긴다 (YouTube API 정책).
    retention_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)

    # 과거 유사 사례 검색용 벡터 (F_R_114).
    # '이 댓글과 비슷한 걸 예전에 관리자가 어떻게 처리했나'를 보여주려는 것.
    # 판단 근거를 사람에게 주는 게 목적이라 같은 채널 안에서만 찾는다 —
    # 채널간 데이터 결합은 YouTube API 정책상 금지다.
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(EMBEDDING_DIM), nullable=True
    )

    # 파이프라인 통과 후의 현재 상태.
    #   pending  아직 판별 안 함
    #   passed   그대로 공개
    #   queued   검토 큐 (관리자가 봐야 함)
    #   hidden   가려짐
    # 관리자가 조치하면 여기가 바뀌고 actions 에 이력이 남는다.
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)

    # 관리자가 처리한 시각. NULL 이면 아직 안 본 것 —
    # 검토 큐는 status='queued' AND reviewed_at IS NULL 로 뽑는다.
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # 유튜브 쪽 검토 상태 (2026-11-04). 위 status 는 Veil 의 상태라 다른 값이다.
    #   heldForReview  유튜브(또는 스튜디오 설정)가 보류해 둔 댓글 — 시청자에게 안 보인다
    #   likelySpam     유튜브가 스팸 의심으로 빼 둔 댓글 — 역시 안 보인다
    #   published / rejected  보류 댓글을 Veil 에서 공개·숨김한 뒤
    #   NULL           공개 목록에서 받은 평범한 댓글. API 키로 받으면 이 값이 안 온다
    youtube_moderation: Mapped[str | None] = mapped_column(String(16), nullable=True)


class RiskAssessment(Base):
    """판별 결과 1건.

    댓글 하나에 여러 행이 쌓일 수 있다 — 프롬프트를 고쳐 재판별하면
    이전 판정을 지우지 않고 새 행을 넣는다. 무엇이 언제 왜 바뀌었는지가
    남아야 관리자에게 설명할 수 있고, 우리도 프롬프트 변경 효과를 본다.
    """

    __tablename__ = "risk_assessments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    comment_id: Mapped[int] = mapped_column(ForeignKey("comments.id"), index=True)

    # 최종 결정을 내린 단계: rule(관리자 등록어) / prefilter(앞단 규칙·ML) / llm
    stage: Mapped[str] = mapped_column(String(10))

    # 앞단 ML 이 본 유해 확률. shadow 기간에는 llm 행에도 남아서, 켰다면
    # 무엇을 GPT 없이 통과시켰을지 실데이터로 잴 수 있다.
    prefilter_score: Mapped[float | None] = mapped_column(Float, nullable=True)

    # 이 판정이 댓글을 어디로 보냈나 (hidden / queue_judge / queue_info / passed)
    destination: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)

    # LLM 결과. 규칙에서 끝났으면 비어 있다.
    risk_level: Mapped[str | None] = mapped_column(String(16), nullable=True)   # harmful/ambiguous/safe
    category: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # LLM 이 매긴 위험도 1~5 (2026-11-09). safe 이거나 이 기능 전의 판정이면 NULL —
    # 그때는 카테고리 표로 대신한다 (services/severity.py).
    severity: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str | None] = mapped_column(String(40), nullable=True)

    # 어떤 기준으로 매긴 판정인지. 프롬프트와 채널 맥락에서 뽑은 지문이다.
    # 이게 없으면 프롬프트를 고친 뒤 "이 판정은 구기준인가 신기준인가"를
    # 알 수 없다. 실제로 4,568건이 그렇게 섞여 구분이 불가능했다.
    prompt_version: Mapped[str | None] = mapped_column(
        String(16), nullable=True, index=True
    )

    # 규칙에 걸렸으면 무엇에 걸렸는지. 관리자에게 "이 단어 때문"이라고
    # 말해줄 수 있어야 해서 값까지 같이 남긴다.
    rule_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rule_value: Mapped[str | None] = mapped_column(String(255), nullable=True)
    rule_action: Mapped[str | None] = mapped_column(String(10), nullable=True)

    created_at: Mapped[datetime | None] = mapped_column(
        DateTime, server_default=func.now()
    )


class Action(Base):
    """관리자 조치 이력 (F_R_115).

    유튜브 반영은 채널 소유자가 OAuth 로 연동한 채널에서만 된다. 연동 안 된
    채널(API 키로 수집만 한 것)은 DB 에만 기록된다 — youtube_synced 가 그
    구분이다.
    """

    __tablename__ = "actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    comment_id: Mapped[int] = mapped_column(ForeignKey("comments.id"), index=True)

    # hide(숨김) / keep(유지) / ban_author(채널차단)
    action_type: Mapped[str] = mapped_column(String(20))

    actor: Mapped[str | None] = mapped_column(String(100), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 유튜브에 실제로 반영됐는지. 미연동 채널이거나 호출이 실패하면 False 고,
    # 그 이유는 note 에 남는다.
    youtube_synced: Mapped[bool] = mapped_column(Boolean, default=False)

    executed_at: Mapped[datetime | None] = mapped_column(
        DateTime, server_default=func.now()
    )


class Evidence(Base):
    """고소용 증거 — 그 순간의 댓글을 고정해 둔 것 (2026-11-09).

    comments 행은 증거가 못 된다.
      1. 작성자가 고치면 수집이 원문을 덮어쓴다 (collector.save_comments).
      2. 숨기고 나면(rejected) 유튜브 API 로 다시 읽을 방법이 없다.
    그래서 원문·작성자·시각·링크를 따로 떼어 두고, 이후로는 고치지 않는다.
    같은 댓글이 수정돼 다시 저장되면 새 행이 생긴다 (해시가 달라서) — 고치기 전 글이 남는다.

    보관은 원래 댓글과 같은 기한까지다. 유튜브 API 정책(Developer Policies III.E.4)상
    우리 서버는 30일 넘게 들고 있을 수 없다. 모욕죄 고소 기간(6개월)을 맞추려면 관리자가
    그 안에 내려받아 직접 보관해야 한다 — 화면에 남은 날짜를 띄우는 이유다.
    """

    __tablename__ = "evidence"
    __table_args__ = (UniqueConstraint("comment_id", "content_hash", name="ux_evidence_comment_hash"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"), index=True)
    comment_id: Mapped[int] = mapped_column(ForeignKey("comments.id", ondelete="CASCADE"), index=True)

    youtube_comment_id: Mapped[str] = mapped_column(String(64))
    video_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    video_title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    author_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    author_channel_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    content: Mapped[str] = mapped_column(Text)
    # 답글이면 무엇에 단 답인지. 모욕인지 아닌지가 거기서 갈린다.
    parent_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    content_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # 저장할 때의 AI 판정. 증거 자체가 아니라 관리자가 고를 때 참고한 것.
    label: Mapped[str | None] = mapped_column(String(16), nullable=True)
    category: Mapped[str | None] = mapped_column(String(20), nullable=True)
    severity: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)

    source: Mapped[str] = mapped_column(String(10))   # auto(판별 즉시) / manual(관리자가 누름)
    saved_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 댓글 ID·작성자·작성 시각·원문의 SHA-256. 내려받은 뒤 고쳐졌는지 확인하는 데 쓴다.
    content_hash: Mapped[str] = mapped_column(String(64))
    captured_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)


class LabelTask(Base):
    """사람이 매기는 정답 — 한 사람에게 배정된 댓글 하나.

    AI 판정이 맞았는지 잴 기준이 없어서 만든다. 답지를 AI 가 만들면 자기
    답을 답지로 삼는 셈이라 항상 100점이 나온다.

    같은 댓글이 여러 사람에게 배정될 수 있다 (segment='공통'). 그 구간에서
    사람끼리 얼마나 갈리는지가 AI 정확도의 천장이다 — 사람도 못 정하는 걸
    AI 가 맞힐 수는 없다.

    라벨링 화면(/label)은 이 표만 읽고 쓴다. AI 판정(risk_assessments)은
    절대 화면에 내보내지 않는다 — 보이면 사람이 따라간다.
    """

    __tablename__ = "label_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    labeler: Mapped[str] = mapped_column(String(50), index=True)
    comment_id: Mapped[int] = mapped_column(ForeignKey("comments.id"), index=True)
    # 그 사람 목록 안에서의 순서. 공통 구간을 앞에 두어 중간에 그만둬도
    # 일치도 계산은 되게 한다.
    position: Mapped[int] = mapped_column(Integer)
    segment: Mapped[str] = mapped_column(String(10))   # 공통 / 개별
    # 어디서 뽑았나. 큐에서 뽑은 것과 통과분에서 뽑은 것은 실제 비율이 달라
    # 채점할 때 가중해야 한다.
    source: Mapped[str] = mapped_column(String(10))    # 큐 / 통과

    # hide / keep / unsure. NULL 이면 아직 안 함.
    label: Mapped[str | None] = mapped_column(String(10), nullable=True)
    labeled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 화면에 뜬 뒤 버튼까지 걸린 초. '관리자가 댓글 하나 보는 데 몇 초' 의
    # 유일한 실측이다.
    seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
