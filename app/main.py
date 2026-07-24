from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from playwright.async_api import Error as PlaywrightError

from app.api.routes import router
from app.core.config import settings
from app.core.database import database
from app.services.listing_manager import listing_manager
from app.services.scheduler import background_scheduler
from app.services.steam_session import steam_session_service

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    database.initialize()
    database.clear_runtime_cache()
    session = await steam_session_service.restore()
    if session.wallet_currency:
        database.save_currency(session.wallet_currency)
    if session.state.value == "logged_in":
        try:
            await listing_manager.refresh_current_listings()
        except (OSError, PlaywrightError, RuntimeError):
            # 启动刷新失败时保持空列表，用户可在界面手动重试。
            pass
    background_scheduler.start()
    yield
    await background_scheduler.stop()


app = FastAPI(title=settings.app_name, version="1.0.0", lifespan=lifespan)
app.include_router(router)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.exception_handler(PlaywrightError)
async def playwright_error_handler(_request: Request, _exc: PlaywrightError) -> JSONResponse:
    """返回脱敏错误，避免 Playwright 调用日志中的请求 Cookie 出现在控制台。"""
    return JSONResponse(
        status_code=503,
        content={
            "detail": "Steam 连接或页面加载异常，请检查 VPN/加速器、重新登录后再试"
        },
    )


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
