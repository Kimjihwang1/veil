"""ORM 모델과 실제 테이블의 컬럼을 맞춘다.

초안 테이블이 이미 만들어져 있어서 ADD COLUMN 으로 따라가는 중이다.
(마이그레이션 도구는 아직 안 붙였으므로 임시 스크립트. 테이블이 더 늘어나면
 Alembic 으로 옮겨야 한다.)

    python -m scripts.init_db
"""

import asyncio

from sqlalchemy import text

from app.db.session import engine

STATEMENTS = [
    # ── MVP① 규칙기반 (F_R_108) ──
    "ALTER TABLE channel_rules ADD COLUMN IF NOT EXISTS compiled_regex TEXT",
    "ALTER TABLE channel_rules ADD COLUMN IF NOT EXISTS action VARCHAR(10) DEFAULT 'block'",
    "ALTER TABLE channel_rules ADD COLUMN IF NOT EXISTS expand_variants BOOLEAN DEFAULT TRUE",
    "UPDATE channel_rules SET compiled_regex = rule_value WHERE compiled_regex IS NULL",
    "ALTER TABLE channel_rules ALTER COLUMN compiled_regex SET NOT NULL",

    # ── 채널별 설정 ──
    # LLM 프롬프트 뒤에 붙일 채널 맥락 (은어 뜻 등)
    "ALTER TABLE channels ADD COLUMN IF NOT EXISTS context TEXT",
    # 사람을 안 거치고 바로 가릴 분류. 콤마로 구분. NULL이면 자동 숨김 없음.
    # 기본값을 주지 않는다 — 기존 채널의 동작이 조용히 바뀌면 안 된다.
    "ALTER TABLE channels ADD COLUMN IF NOT EXISTS auto_hide_categories TEXT",

    # ── 로그인·권한 (F_R_101) ──
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS picture TEXT",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login_at TIMESTAMP",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash TEXT",
    "ALTER TABLE workspaces ALTER COLUMN type SET DEFAULT 'personal'",
    "ALTER TABLE workspace_members ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT now()",
    # 같은 사람이 같은 워크스페이스에 두 번 들어가면 권한 판정이 갈린다.
    """CREATE UNIQUE INDEX IF NOT EXISTS ux_workspace_member
       ON workspace_members (workspace_id, user_id)""",

    # 세션을 DB 에 두는 이유는 models.Session 주석 참고 (즉시 회수 가능).
    """CREATE TABLE IF NOT EXISTS sessions (
        token        VARCHAR(64) PRIMARY KEY,
        user_id      INTEGER NOT NULL REFERENCES users(id),
        created_at   TIMESTAMP DEFAULT now(),
        expires_at   TIMESTAMP NOT NULL,
        last_seen_at TIMESTAMP
    )""",
    "CREATE INDEX IF NOT EXISTS ix_sessions_user ON sessions (user_id)",
    "CREATE INDEX IF NOT EXISTS ix_sessions_expires ON sessions (expires_at)",

    # ── 채널 연동 (F_R_102) ──
    "ALTER TABLE channels ADD COLUMN IF NOT EXISTS ai_consent_at TIMESTAMP",
    "ALTER TABLE channels ADD COLUMN IF NOT EXISTS connected_by_user_id INTEGER REFERENCES users(id)",
    # 채널 소유자 리프레시 토큰. 연동 해제 시 반드시 NULL 로 지운다.
    "ALTER TABLE channels ADD COLUMN IF NOT EXISTS youtube_refresh_token TEXT",
    "CREATE INDEX IF NOT EXISTS ix_channels_workspace ON channels (workspace_id)",
    # 채널 연결 초대 링크. 왜 DB 인지는 models.ChannelInvite 주석 참고.
    """CREATE TABLE IF NOT EXISTS channel_invites (
        id                 SERIAL PRIMARY KEY,
        token              VARCHAR(64) UNIQUE NOT NULL,
        workspace_id       INTEGER NOT NULL REFERENCES workspaces(id),
        created_by_user_id INTEGER NOT NULL REFERENCES users(id),
        note               VARCHAR(100),
        created_at         TIMESTAMP DEFAULT now(),
        expires_at         TIMESTAMP NOT NULL,
        used_at            TIMESTAMP,
        result             VARCHAR(500)
    )""",
    "CREATE INDEX IF NOT EXISTS ix_channel_invites_ws ON channel_invites (workspace_id)",


    # ── 수집 대상 영상 ──
    # 어떤 사건의 영상인지(topic)를 남겨야 나중에 주제별로 나눠 볼 수 있다.
    """CREATE TABLE IF NOT EXISTS videos (
        video_id      VARCHAR(64) PRIMARY KEY,
        channel_id    INTEGER NOT NULL REFERENCES channels(id),
        title         VARCHAR(500),
        topic         VARCHAR(40),
        comment_count INTEGER,
        published_at  TIMESTAMP,
        collected_at  TIMESTAMP DEFAULT now()
    )""",
    "CREATE INDEX IF NOT EXISTS ix_videos_channel ON videos (channel_id)",
    "CREATE INDEX IF NOT EXISTS ix_videos_topic ON videos (topic)",
    # 영상별 맥락. 채널 맥락과 나눈 이유는 models.Video.context 주석 참고.
    "ALTER TABLE videos ADD COLUMN IF NOT EXISTS context TEXT",

    # ── 사람 라벨링 (정답 세트) ──
    # 누가 어느 댓글을 봤고 뭘 눌렀나. AI 판정은 여기 없다 — models.LabelTask 주석 참고.
    """CREATE TABLE IF NOT EXISTS label_tasks (
        id          SERIAL PRIMARY KEY,
        labeler     VARCHAR(50) NOT NULL,
        comment_id  INTEGER NOT NULL REFERENCES comments(id),
        position    INTEGER NOT NULL,
        segment     VARCHAR(10) NOT NULL,
        source      VARCHAR(10) NOT NULL,
        label       VARCHAR(10),
        labeled_at  TIMESTAMP,
        seconds     DOUBLE PRECISION
    )""",
    "CREATE INDEX IF NOT EXISTS ix_label_tasks_labeler ON label_tasks (labeler)",
    "CREATE INDEX IF NOT EXISTS ix_label_tasks_comment_id ON label_tasks (comment_id)",

    # ── 댓글 수집기 (F_R_107) ──
    # 답글 지원
    "ALTER TABLE comments ADD COLUMN IF NOT EXISTS parent_comment_id VARCHAR(64)",
    "ALTER TABLE comments ADD COLUMN IF NOT EXISTS is_reply BOOLEAN DEFAULT FALSE",
    "CREATE INDEX IF NOT EXISTS ix_comments_parent ON comments (parent_comment_id)",

    # 우선순위 신호 (확산도 · 논쟁 강도)
    "ALTER TABLE comments ADD COLUMN IF NOT EXISTS like_count INTEGER DEFAULT 0",
    "ALTER TABLE comments ADD COLUMN IF NOT EXISTS total_reply_count INTEGER DEFAULT 0",

    # 작성자 식별 (반복 악플러 추적 · banAuthor 대상)
    "ALTER TABLE comments ADD COLUMN IF NOT EXISTS author_channel_id VARCHAR(64)",
    "CREATE INDEX IF NOT EXISTS ix_comments_author ON comments (author_channel_id)",

    # 댓글 수정 감지 → 재판별 대상 판단
    "ALTER TABLE comments ADD COLUMN IF NOT EXISTS content_updated_at TIMESTAMP",

    # 30일 보관 정책 (YouTube API 정책). 이후엔 파생 지표만 남긴다.
    "ALTER TABLE comments ADD COLUMN IF NOT EXISTS retention_expires_at TIMESTAMP",
    "CREATE INDEX IF NOT EXISTS ix_comments_retention ON comments (retention_expires_at)",

    # ── 과거 유사 사례 (F_R_114) ──
    # pgvector 를 고른 이유가 이것이다. 단어가 겹치지 않아도 뜻이 비슷하면 찾는다.
    "CREATE EXTENSION IF NOT EXISTS vector",
    "ALTER TABLE comments ADD COLUMN IF NOT EXISTS embedding vector(1536)",
    # 코사인 거리 기준 근사 검색. 데이터가 적을 땐 없어도 되지만,
    # 채널 하나가 수만 건이 되면 인덱스 없이는 매번 전수 비교가 된다.
    """CREATE INDEX IF NOT EXISTS ix_comments_embedding
       ON comments USING hnsw (embedding vector_cosine_ops)""",

    # ── 판별 결과 · 관리자 조치 ──
    # 파이프라인 결과를 DB 에 남겨야 검토 큐 API 가 읽을 게 생긴다.
    "ALTER TABLE comments ADD COLUMN IF NOT EXISTS status VARCHAR(16) DEFAULT 'pending'",
    "ALTER TABLE comments ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMP",
    "CREATE INDEX IF NOT EXISTS ix_comments_status ON comments (status)",

    "ALTER TABLE risk_assessments ADD COLUMN IF NOT EXISTS destination VARCHAR(16)",
    "ALTER TABLE risk_assessments ADD COLUMN IF NOT EXISTS category VARCHAR(20)",
    "ALTER TABLE risk_assessments ADD COLUMN IF NOT EXISTS model VARCHAR(40)",
    "ALTER TABLE risk_assessments ADD COLUMN IF NOT EXISTS rule_id INTEGER",
    "ALTER TABLE risk_assessments ADD COLUMN IF NOT EXISTS rule_value VARCHAR(255)",
    "ALTER TABLE risk_assessments ADD COLUMN IF NOT EXISTS rule_action VARCHAR(10)",
    "CREATE INDEX IF NOT EXISTS ix_ra_dest ON risk_assessments (destination)",
    # 판정 기준의 지문. models.RiskAssessment.prompt_version 주석 참고.
    "ALTER TABLE risk_assessments ADD COLUMN IF NOT EXISTS prompt_version VARCHAR(16)",
    """CREATE INDEX IF NOT EXISTS ix_ra_prompt
       ON risk_assessments (prompt_version)""",
    # 앞단 ML 의 유해 확률. models.RiskAssessment.prefilter_score 주석 참고.
    "ALTER TABLE risk_assessments ADD COLUMN IF NOT EXISTS prefilter_score DOUBLE PRECISION",
    # 'AI 프롬프트' · '자동 감지' 화면 (2026-11-03). models.Channel 주석 참고.
    "ALTER TABLE channels ADD COLUMN IF NOT EXISTS prompt_template TEXT",
    "ALTER TABLE channels ADD COLUMN IF NOT EXISTS watch_enabled BOOLEAN NOT NULL DEFAULT TRUE",
    "ALTER TABLE channels ADD COLUMN IF NOT EXISTS watch_interval_min INTEGER",
    "ALTER TABLE channels ADD COLUMN IF NOT EXISTS last_watched_at TIMESTAMP",
    # 유튜브가 보류·스팸 의심으로 잡아 둔 댓글 (2026-11-04). models.Comment 주석 참고.
    "ALTER TABLE comments ADD COLUMN IF NOT EXISTS youtube_moderation VARCHAR(16)",

    "ALTER TABLE actions ADD COLUMN IF NOT EXISTS actor VARCHAR(100)",
    "ALTER TABLE actions ADD COLUMN IF NOT EXISTS note TEXT",
    "ALTER TABLE actions ADD COLUMN IF NOT EXISTS youtube_synced BOOLEAN DEFAULT FALSE",

    # upsert 의 충돌 기준. 이게 있어야 같은 영상을 다시 돌려도 중복이 안 쌓인다.
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_comments_yt_id ON comments (youtube_comment_id)",

    # ── 위험도 1~5 · 증거 보관 (2026-11-09) ──
    # LLM 이 매긴 위험도. NULL 이면 카테고리 표로 대신한다 (services/severity.py).
    "ALTER TABLE risk_assessments ADD COLUMN IF NOT EXISTS severity SMALLINT",
    # 고소용 증거. 왜 comments 와 따로 두는지는 models.Evidence 주석 참고.
    """CREATE TABLE IF NOT EXISTS evidence (
        id                 SERIAL PRIMARY KEY,
        channel_id         INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
        comment_id         INTEGER NOT NULL REFERENCES comments(id) ON DELETE CASCADE,
        youtube_comment_id VARCHAR(64) NOT NULL,
        video_id           VARCHAR(64),
        video_title        VARCHAR(500),
        author_name        VARCHAR(255),
        author_channel_id  VARCHAR(64),
        content            TEXT NOT NULL,
        parent_content     TEXT,
        published_at       TIMESTAMP,
        content_updated_at TIMESTAMP,
        label              VARCHAR(16),
        category           VARCHAR(20),
        severity           SMALLINT,
        reasoning          TEXT,
        source             VARCHAR(10) NOT NULL,
        saved_by           VARCHAR(100),
        note               TEXT,
        content_hash       VARCHAR(64) NOT NULL,
        captured_at        TIMESTAMP DEFAULT now(),
        expires_at         TIMESTAMP,
        CONSTRAINT ux_evidence_comment_hash UNIQUE (comment_id, content_hash)
    )""",
    "CREATE INDEX IF NOT EXISTS ix_evidence_channel_id ON evidence (channel_id)",
    "CREATE INDEX IF NOT EXISTS ix_evidence_comment_id ON evidence (comment_id)",
    "CREATE INDEX IF NOT EXISTS ix_evidence_author_channel_id ON evidence (author_channel_id)",
    "CREATE INDEX IF NOT EXISTS ix_evidence_expires_at ON evidence (expires_at)",
]


async def main() -> None:
    async with engine.begin() as conn:
        for sql in STATEMENTS:
            await conn.execute(text(sql))
            print(f"[OK] {sql[:72]}")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
