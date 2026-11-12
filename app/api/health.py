"""헬스체크 라우터."""

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db

router = APIRouter(prefix="/health", tags=["health"])


@router.get("")
async def health() -> dict[str, str]:
    """앱 자체가 살아있는지만 확인 (DB 접속 안 함)."""
    return {"status": "ok"}


@router.get("/db")
async def health_db(db: AsyncSession = Depends(get_db)) -> dict[str, object]:
    """DB 접속 + pgvector 익스텐션 활성화 여부까지 확인."""
    version = (await db.execute(text("SELECT version()"))).scalar_one()
    vector_version = (
        await db.execute(
            text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        )
    ).scalar_one_or_none()

    return {
        "status": "ok",
        "postgres": version,
        "pgvector": vector_version or "not installed",
    }


@router.get("/watch")
async def health_watch() -> dict[str, object]:
    """자동 감시가 돌고 있는지. 마지막에 언제 돌았고 뭘 했는지.

    이게 없으면 감시가 멈춰도 아무도 모른다 — 새 댓글이 안 들어오는 걸
    '요즘 댓글이 없나 보다' 로 읽게 된다.
    """
    from app.core.config import get_settings
    from app.services.watcher import 상태

    cfg = get_settings()
    return {
        "enabled": cfg.watch_enabled,
        "interval_seconds": cfg.watch_interval,
        "videos_per_channel": cfg.watch_videos_per_channel,
        "llm_daily_cap": cfg.llm_daily_cap,
        "llm_calls_today": 상태["llm_calls_today"],
        "last_run_at": 상태["last_run_at"],
        "last_result": 상태["last_result"],
    }
