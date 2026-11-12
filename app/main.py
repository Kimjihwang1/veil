"""Outlier 백엔드 진입점."""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.api import auth, channel_tools, channels, evidence, health, insights, label, moderation, review, rules
from app.core.config import get_settings
from app.db.session import engine
from app.services import retention, watcher

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 기동 시점에 DB가 실제로 붙는지 확인한다.
    # 여기서 죽으면 잘못된 접속정보로 서버가 뜨는 걸 막을 수 있다.
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as e:
        # 그냥 두면 asyncpg 스택 트레이스 수십 줄만 나와서, 정작 'DB 가 안
        # 떠 있다'는 사실이 안 보인다. Docker Desktop 이 꺼지면 컨테이너도
        # 같이 멈추기 때문에 개발 중에 자주 겪는다.
        cfg = get_settings()
        raise RuntimeError(
            "\n"
            "  DB 에 연결하지 못했습니다.\n"
            f"    접속 대상 : {cfg.postgres_host}:{cfg.postgres_port}"
            f" / {cfg.postgres_db}\n"
            f"    원인      : {type(e).__name__}: {str(e)[:120]}\n"
            "\n"
            "  대개 컨테이너가 멈춰 있어서입니다. 이것부터 해보세요:\n"
            "    docker start outlier-db\n"
            "\n"
            "  그래도 안 되면 Docker Desktop 자체가 꺼진 것일 수 있습니다.\n"
        ) from None

    # 서버가 스스로 하는 일 둘.
    #   파기 — 보관기한 지난 원문을 지운다. 스크립트만 있으면 잊는 순간 깨진다.
    #   감시 — 연동된 채널의 새 댓글을 긁어 판별한다. 이게 없으면 관리자가
    #          채널을 연결해도 개발자가 스크립트를 돌려줘야 화면에 뭐가 뜬다.
    작업 = [
        asyncio.create_task(retention.run_forever()),
        asyncio.create_task(watcher.run_forever()),
    ]

    yield

    for t in 작업:
        t.cancel()
    for t in 작업:
        try:
            await t
        except asyncio.CancelledError:
            pass
    await engine.dispose()


app = FastAPI(
    title=settings.app_name,
    description="유튜브 다채널 유해댓글 관리 서비스",
    version="0.1.0",
    lifespan=lifespan,
)

# 프론트엔드는 다른 포트에서 돈다. 이게 없으면 브라우저가 API 호출을 막는다.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router, prefix="/api")
app.include_router(auth.router, prefix="/api")
app.include_router(channels.router, prefix="/api")
app.include_router(rules.router, prefix="/api")
app.include_router(moderation.router, prefix="/api")
app.include_router(review.router, prefix="/api")
app.include_router(insights.router, prefix="/api")
app.include_router(channel_tools.router, prefix="/api")
app.include_router(evidence.router, prefix="/api")
app.include_router(label.router, prefix="/api")


STATIC_DIR = Path(__file__).parent / "static"

# 관리자 화면은 빌드 단계 없는 정적 파일이다. 같은 서버에서 내보내므로
# 프론트를 따로 띄울 필요도, CORS 를 탈 일도 없다.
@app.middleware("http")
async def _static_no_cache(request, call_next):
    """js·css 도 매번 서버에 물어보게 한다.

    StaticFiles 는 ETag 만 붙이고 Cache-Control 을 안 준다. 그러면 브라우저가
    Last-Modified 를 보고 알아서 며칠씩 들고 있어서, 화면을 고쳐도 사용자는
    옛 app.js 를 본다 (2026-09-17 실제로 그랬다 — "뭐가 다른 거야, 똑같은데").
    no-cache 는 '쓰지 마라' 가 아니라 '쓰기 전에 물어봐라' 다. ETag 가 같으면
    304 로 끝나서 비용은 거의 없다.
    """
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# 온보딩 1~6. 디자인 캔버스의 아트보드를 그대로 옮긴 정적 화면이다.
# 실제 인증은 아직 없다 — Google/YouTube OAuth 가 붙기 전까지는 클릭으로 넘어간다.
ONBOARDING = {
    1: "1-login.html",
    2: "2-signup.html",
    3: "3-connect.html",
    4: "4-select.html",
    5: "5-consent.html",
    6: "6-done.html",
}


def _page(path: Path) -> FileResponse:
    """HTML 은 브라우저가 들고 있지 않게 한다.

    캐시 헤더가 없으면 브라우저가 알아서 오래 들고 있는다. 그래서 화면을
    고쳐도 옛날 사이드바가 그대로 뜨는 일이 생긴다 — 실제로 겪었다.
    no-cache 는 '쓰지 마라'가 아니라 '쓰기 전에 물어봐라'다. 안 바뀌었으면
    304 로 끝나서 비용도 거의 없다.

    /static 아래 js·css 는 _static_no_cache 미들웨어가 같은 헤더를 붙인다.
    """
    return FileResponse(path, headers={"Cache-Control": "no-cache"})


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    """첫 화면은 로그인. 프로토타입 흐름이 여기서 시작한다."""
    return _page(STATIC_DIR / "screens" / ONBOARDING[1])


@app.get("/onboarding/{step}", include_in_schema=False)
async def onboarding(step: int) -> FileResponse:
    if step not in ONBOARDING:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"온보딩 {step}단계 없음")
    return _page(STATIC_DIR / "screens" / ONBOARDING[step])


@app.get("/connect/done", include_in_schema=False)
@app.get("/connect/{token}", include_in_schema=False)
async def invite_page() -> FileResponse:
    """유튜버가 받는 채널 연결 초대 페이지. 로그인 없음.

    /connect/<토큰> 은 안내 + [권한 주기], /connect/done 은 결과. 한 파일이
    주소를 보고 둘 중 하나를 그린다.
    """
    return _page(STATIC_DIR / "invite.html")


@app.get("/app", include_in_schema=False)
async def admin() -> FileResponse:
    """관리자 화면 (대시보드·검토 큐·숨김·관리 기준·이력)."""
    return _page(STATIC_DIR / "app.html")


@app.get("/label", include_in_schema=False)
async def label_page() -> FileResponse:
    """사람 라벨링. 팀 내부용 — 인증 없음. AI 판정은 화면에 안 나온다."""
    return _page(STATIC_DIR / "label.html")
