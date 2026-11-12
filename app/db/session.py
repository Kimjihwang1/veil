"""DB 엔진 / 세션 관리."""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings

settings = get_settings()

engine: AsyncEngine = create_async_engine(
    settings.database_url,
    echo=settings.db_echo,
    pool_size=5,
    max_overflow=10,
    # 커넥션이 끊긴 채로 풀에 남아있는 경우를 걸러낸다.
    pool_pre_ping=True,
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 의존성. 라우터에서 `db: AsyncSession = Depends(get_db)` 로 받는다."""
    async with AsyncSessionLocal() as session:
        yield session
