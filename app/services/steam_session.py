import asyncio
from collections.abc import Iterable
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import unquote

from playwright.async_api import BrowserContext, Cookie, async_playwright
from playwright.async_api import Error as PlaywrightError

from app.core.config import settings
from app.core.models import Currency, SessionState, SessionStatus

STEAM_LOGIN_URL = (
    "https://steamcommunity.com/login/home/"
    "?goto=market%2F&redir=https%3A%2F%2Fsteamcommunity.com%2Fmarket%2F"
)
STEAM_MARKET_URL = "https://steamcommunity.com/market/"
STEAM_LOGIN_COOKIE = "steamLoginSecure"


def extract_steam_id(cookies: Iterable[Cookie | dict[str, object]]) -> str | None:
    """从 Steam 登录 Cookie 中提取 SteamID，不记录 Cookie 原文。"""
    for cookie in cookies:
        if cookie.get("name") != STEAM_LOGIN_COOKIE:
            continue
        value = unquote(str(cookie.get("value", "")))
        steam_id, separator, _token = value.partition("||")
        if separator and steam_id.isdigit():
            return steam_id
    return None


class SteamSessionService:
    """通过 Playwright 持久化浏览器配置恢复 Steam 官方网页登录会话。"""

    def __init__(self, profile_dir: Path | None = None) -> None:
        self.profile_dir = profile_dir or settings.steam_profile_dir
        self._lock = asyncio.Lock()
        self._status = SessionStatus(
            state=SessionState.LOGIN_REQUIRED,
            message="首次使用需要在 Steam 官方登录窗口中登录",
        )

    def status(self) -> SessionStatus:
        return self._status.model_copy()

    @asynccontextmanager
    async def browser_context(self, *, headless: bool = True):
        """为库存、行情和市场服务提供共享登录会话，防止配置目录并发占用。"""
        async with self._lock:
            if self._status.state is not SessionState.LOGGED_IN:
                raise RuntimeError("Steam 尚未登录")
            playwright, context = await self._open_context(headless=headless)
            try:
                yield context
            finally:
                await context.close()
                await playwright.stop()

    async def _open_context(self, *, headless: bool) -> tuple[object, BrowserContext]:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        playwright = await async_playwright().start()
        try:
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=self.profile_dir,
                headless=headless,
                viewport={"width": 1180, "height": 820},
                locale="zh-CN",
                args=["--disable-extensions"],
            )
        except Exception:
            await playwright.stop()
            raise
        return playwright, context

    async def _authenticated_status(self, context: BrowserContext) -> SessionStatus | None:
        steam_id = extract_steam_id(await context.cookies(["https://steamcommunity.com"]))
        if not steam_id:
            return None

        page = context.pages[0] if context.pages else await context.new_page()
        response = await page.goto(STEAM_MARKET_URL, wait_until="domcontentloaded", timeout=45_000)
        if response is None or response.status >= 400 or "/login/" in page.url:
            return None
        wallet_info = await page.evaluate(
            """
            () => {
              const wallet = window.g_rgWalletInfo || {};
              return {
                currency: Number(wallet.wallet_currency || 0),
                feePercent: Number(wallet.wallet_fee_percent ?? 0.05),
                feeMinimum: Number(wallet.wallet_fee_minimum ?? 1),
                feeBase: Number(wallet.wallet_fee_base ?? 0),
                publisherFee: Number(
                  wallet.wallet_publisher_fee_percent_default ?? 0.10
                )
              };
            }
            """
        )
        wallet_currency_id = int(wallet_info["currency"])
        wallet_currency = {23: Currency.CNY, 24: Currency.INR}.get(wallet_currency_id)
        return SessionStatus(
            state=SessionState.LOGGED_IN,
            steam_id=steam_id,
            wallet_currency=wallet_currency,
            wallet_fee_percent=float(wallet_info["feePercent"]),
            wallet_fee_minimum=int(wallet_info["feeMinimum"]),
            wallet_fee_base=int(wallet_info["feeBase"]),
            wallet_publisher_fee_percent_default=float(wallet_info["publisherFee"]),
            message="Steam 会话有效，下次启动将自动恢复",
        )

    async def restore(self) -> SessionStatus:
        """启动时从本地浏览器配置恢复并在线验证会话。"""
        async with self._lock:
            if not self.profile_dir.exists():
                return self.status()
            try:
                playwright, context = await self._open_context(headless=True)
                try:
                    restored = await self._authenticated_status(context)
                finally:
                    await context.close()
                    await playwright.stop()
            except (OSError, PlaywrightError) as exc:
                self._status = SessionStatus(
                    state=SessionState.EXPIRED,
                    message=f"无法验证已保存的 Steam 会话：{type(exc).__name__}",
                )
                return self.status()

            self._status = restored or SessionStatus(
                state=SessionState.EXPIRED,
                message="已保存的 Steam 会话无效，请重新登录",
            )
            return self.status()

    async def login(self) -> SessionStatus:
        """打开真实 Steam 官方页面，等待用户完成密码及 Steam Guard 验证。"""
        async with self._lock:
            self._status = SessionStatus(
                state=SessionState.LOGGING_IN,
                message="请在弹出的 Steam 官方窗口中完成登录",
            )
            try:
                playwright, context = await self._open_context(headless=False)
            except (OSError, PlaywrightError) as exc:
                self._status = SessionStatus(
                    state=SessionState.LOGIN_REQUIRED,
                    message=(
                        "无法启动内置浏览器，请先执行 "
                        "playwright install chromium；"
                        f"错误类型：{type(exc).__name__}"
                    ),
                )
                return self.status()

            page = context.pages[0] if context.pages else await context.new_page()
            try:
                await page.goto(STEAM_LOGIN_URL, wait_until="domcontentloaded", timeout=45_000)
                deadline = asyncio.get_running_loop().time() + settings.login_timeout_seconds
                while asyncio.get_running_loop().time() < deadline:
                    authenticated = await self._authenticated_status(context)
                    if authenticated:
                        self._status = authenticated
                        return self.status()
                    await asyncio.sleep(1)
                self._status = SessionStatus(
                    state=SessionState.LOGIN_REQUIRED,
                    message="登录等待超时，请重新发起登录",
                )
                return self.status()
            except (OSError, PlaywrightError) as exc:
                self._status = SessionStatus(
                    state=SessionState.LOGIN_REQUIRED,
                    message=f"Steam 登录窗口异常关闭：{type(exc).__name__}",
                )
                return self.status()
            finally:
                await context.close()
                await playwright.stop()

    async def logout(self) -> SessionStatus:
        """清除本地持久化浏览器中的 Steam Cookie。"""
        async with self._lock:
            try:
                playwright, context = await self._open_context(headless=True)
                try:
                    await context.clear_cookies()
                    for page in context.pages:
                        await page.evaluate("localStorage.clear(); sessionStorage.clear()")
                finally:
                    await context.close()
                    await playwright.stop()
            except (OSError, PlaywrightError):
                # 即使浏览器无法启动，也不声称已经清理成功。
                self._status = SessionStatus(
                    state=SessionState.EXPIRED,
                    message="无法打开本地会话存储，退出操作未完成",
                )
                return self.status()

            self._status = SessionStatus(
                state=SessionState.LOGGED_OUT,
                message="本地 Steam 登录会话已清除",
            )
            return self.status()


steam_session_service = SteamSessionService()
