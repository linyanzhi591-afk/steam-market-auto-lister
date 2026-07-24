import asyncio
import contextlib

from playwright.async_api import Error as PlaywrightError

from app.core.config import settings
from app.core.models import Currency, SessionState
from app.services.listing_manager import listing_manager
from app.services.steam_session import steam_session_service


class BackgroundScheduler:
    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="steam-market-scheduler")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(settings.sync_interval_seconds)
            if steam_session_service.status().state is not SessionState.LOGGED_IN:
                continue
            try:
                await listing_manager.sync_states()
                if settings.allow_market_writes and not settings.dry_run:
                    await listing_manager.process_expired(Currency(settings.default_currency))
            except (OSError, PermissionError, PlaywrightError, RuntimeError):
                # 后台失败不得终止服务；具体失败由下一轮或人工同步重新检查。
                continue


background_scheduler = BackgroundScheduler()
