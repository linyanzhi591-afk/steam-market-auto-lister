import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from playwright.async_api import APIResponse
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.core.config import settings
from app.core.database import Database, database
from app.core.models import Currency, InventoryAsset, SyncResult
from app.services.steam_session import SteamSessionService, steam_session_service

CURRENCY_IDS = {Currency.CNY: 23, Currency.INR: 24}
CURRENCY_SYMBOLS = {
    Currency.CNY: {"¥", "￥", "元"},
    Currency.INR: {"₹"},
}


def _description_key(classid: str, instanceid: str) -> str:
    return f"{classid}_{instanceid}"


def parse_inventory_payload(
    appid: int, contextid: str, payload: dict[str, Any]
) -> list[InventoryAsset]:
    descriptions = {
        _description_key(str(item["classid"]), str(item.get("instanceid", "0"))): item
        for item in payload.get("descriptions", [])
    }
    result: list[InventoryAsset] = []
    for asset in payload.get("assets", []):
        description = descriptions.get(
            _description_key(str(asset["classid"]), str(asset.get("instanceid", "0"))), {}
        )
        result.append(
            InventoryAsset(
                appid=appid,
                contextid=contextid,
                assetid=str(asset["assetid"]),
                classid=str(asset["classid"]),
                instanceid=str(asset.get("instanceid", "0")),
                amount=int(asset.get("amount", 1)),
                name=str(description.get("name", "未知物品")),
                market_hash_name=str(
                    description.get("market_hash_name") or description.get("name", "未知物品")
                ),
                marketable=bool(description.get("marketable", 0)),
                tradable=bool(description.get("tradable", 0)),
                commodity=bool(description.get("commodity", 0)),
                icon_url=description.get("icon_url"),
            )
        )
    return result


def parse_price_history(payload: dict[str, Any]) -> list[tuple[str, int, int]]:
    cutoff = datetime.now(UTC) - timedelta(days=30)
    rows: list[tuple[str, int, int]] = []
    for raw in payload.get("prices", []):
        if not isinstance(raw, list) or len(raw) < 3:
            continue
        try:
            timestamp = datetime.strptime(str(raw[0])[:15], "%b %d %Y %H:").replace(tzinfo=UTC)
            if timestamp < cutoff:
                continue
            price_minor = round(float(raw[1]) * 100)
            volume = int(str(raw[2]).replace(",", ""))
        except (TypeError, ValueError):
            continue
        rows.append((timestamp.isoformat(), price_minor, volume))
    return rows


class SteamMarketService:
    def __init__(
        self,
        session: SteamSessionService | None = None,
        store: Database | None = None,
    ) -> None:
        self.session = session or steam_session_service
        self.store = store or database

    async def _get_with_retry(
        self,
        request_context: Any,
        url: str,
        *,
        params: dict[str, object] | None = None,
    ) -> APIResponse:
        last_error: PlaywrightError | None = None
        for attempt in range(settings.request_retries + 1):
            try:
                return await request_context.get(
                    url,
                    params=params,
                    timeout=settings.request_timeout_seconds * 1000,
                )
            except PlaywrightError as exc:
                last_error = exc
                if attempt >= settings.request_retries:
                    break
                await asyncio.sleep(2**attempt)
        raise RuntimeError(
            f"连接 Steam 超时，已重试 {settings.request_retries} 次；"
            "请检查 Steam 社区网络或加速器"
        ) from last_error

    async def _inventory_contexts(self, context: Any, steam_id: str) -> list[tuple[int, str]]:
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(
            f"https://steamcommunity.com/profiles/{steam_id}/inventory/",
            wait_until="domcontentloaded",
            timeout=45_000,
        )
        try:
            await page.wait_for_function(
                "() => Object.keys(window.g_rgAppContextData || {}).length > 0",
                timeout=15_000,
            )
        except PlaywrightTimeoutError:
            pass
        contexts = await page.evaluate(
            """
            () => {
              const apps = window.g_rgAppContextData || {};
              const fromGlobal = Object.entries(apps).flatMap(([appidKey, app]) =>
                Object.keys(app.rgContexts || {}).map(contextid => [
                  Number(app.appid || appidKey), String(contextid)
                ])
              );
              const fromDom = Array.from(document.querySelectorAll('[data-appid]'))
                .flatMap(node => {
                  const appid = Number(node.dataset.appid);
                  const contextid = node.dataset.contextid || node.getAttribute('data-context-id');
                  return appid && contextid ? [[appid, String(contextid)]] : [];
                });
              return Array.from(
                new Map([...fromGlobal, ...fromDom].map(pair => [pair.join('_'), pair])).values()
              );
            }
            """
        )
        result = [(int(appid), str(contextid)) for appid, contextid in contexts]
        if not result:
            raise RuntimeError(
                "Steam 库存页面未返回任何游戏上下文；请确认库存不是完全私密，"
                "刷新 Steam 登录后重试"
            )
        return result

    async def _page_json_with_retry(
        self,
        page: Any,
        url: str,
        *,
        params: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        """在已登录 Steam 页面内请求，复用浏览器的 Cookie、VPN 和代理网络栈。"""
        last_error = "未知网络错误"
        attempts = 0
        for attempt in range(settings.request_retries + 1):
            attempts = attempt + 1
            result = await page.evaluate(
                """
                async ({url, params, timeoutMs}) => {
                  const controller = new AbortController();
                  const timer = setTimeout(() => controller.abort(), timeoutMs);
                  try {
                    const target = new URL(url);
                    for (const [key, value] of Object.entries(params || {})) {
                      target.searchParams.set(key, String(value));
                    }
                    const response = await fetch(target.toString(), {
                      credentials: 'include',
                      signal: controller.signal,
                      headers: {'Accept': 'application/json'}
                    });
                    const text = await response.text();
                    let payload = null;
                    try {
                      payload = JSON.parse(text);
                    } catch {
                      return {
                        ok: false,
                        status: response.status,
                        error: `Steam 返回了非 JSON 内容（HTTP ${response.status}）`
                      };
                    }
                    return {ok: response.ok, status: response.status, payload, error: null};
                  } catch (error) {
                    return {
                      ok: false,
                      status: 0,
                      error: error?.name === 'AbortError' ? '请求超时' : '浏览器网络请求失败'
                    };
                  } finally {
                    clearTimeout(timer);
                  }
                }
                """,
                {
                    "url": url,
                    "params": params or {},
                    "timeoutMs": settings.request_timeout_seconds * 1000,
                },
            )
            if result.get("ok"):
                return dict(result["payload"])
            last_error = str(result.get("error") or f"HTTP {result.get('status', 0)}")
            status = int(result.get("status", 0))
            retryable = status == 0 or status == 429 or status >= 500
            if not retryable or attempt >= settings.request_retries:
                break
            await asyncio.sleep(2**attempt)
        raise RuntimeError(
            f"{last_error}，共尝试 {attempts} 次；"
            "请检查 Steam 社区连接或 VPN/加速器规则"
        )

    async def _navigate_json_with_retry(
        self,
        page: Any,
        url: str,
        *,
        params: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        """直接导航至 JSON 接口，绕过 Steam 页面对脚本 fetch 的限制。"""
        target = f"{url}?{urlencode(params or {})}"
        last_error = "未知网络错误"
        for attempt in range(settings.request_retries + 1):
            try:
                response = await page.goto(
                    target,
                    wait_until="domcontentloaded",
                    timeout=settings.request_timeout_seconds * 1000,
                )
                if response is None:
                    last_error = "Steam 未返回响应"
                elif not response.ok:
                    last_error = f"Steam 返回 HTTP {response.status}"
                    if response.status < 500:
                        break
                else:
                    body = await page.text_content("body")
                    if not body:
                        last_error = "Steam 返回空响应"
                    else:
                        try:
                            payload = json.loads(body)
                        except json.JSONDecodeError:
                            last_error = "Steam 返回了无法识别的价格数据"
                        else:
                            if isinstance(payload, dict):
                                return payload
                            last_error = "Steam 返回的价格数据结构不正确"
            except PlaywrightTimeoutError:
                last_error = "请求超时"
            except PlaywrightError:
                last_error = "浏览器导航失败"
            if attempt < settings.request_retries:
                await asyncio.sleep(2**attempt)
        raise RuntimeError(
            f"{last_error}，已重试 {settings.request_retries} 次；"
            "请检查 Steam 市场页面能否在登录窗口中正常打开"
        )

    async def scan_inventory(self) -> SyncResult:
        status = self.session.status()
        if not status.steam_id:
            raise RuntimeError("Steam 会话中缺少 SteamID")
        assets: list[InventoryAsset] = []
        errors: list[str] = []
        async with self.session.browser_context() as context:
            inventory_contexts = await self._inventory_contexts(context, status.steam_id)
            page = context.pages[0] if context.pages else await context.new_page()
            await asyncio.sleep(settings.request_delay_seconds)
            for appid, contextid in inventory_contexts:
                start_assetid: str | None = None
                while True:
                    params: dict[str, object] = {
                        "l": "schinese",
                        "count": settings.inventory_page_size,
                    }
                    if start_assetid:
                        params["start_assetid"] = start_assetid
                    try:
                        payload = await self._page_json_with_retry(
                            page,
                            (
                                f"https://steamcommunity.com/inventory/"
                                f"{status.steam_id}/{appid}/{contextid}"
                            ),
                            params=params,
                        )
                    except RuntimeError as exc:
                        errors.append(f"{appid}/{contextid}: {exc}")
                        break
                    if not payload.get("success"):
                        errors.append(f"{appid}/{contextid}: Steam 返回库存读取失败")
                        break
                    assets.extend(parse_inventory_payload(appid, contextid, payload))
                    if not payload.get("more_items"):
                        break
                    start_assetid = str(payload.get("last_assetid", ""))
                    if not start_assetid:
                        break
                    await asyncio.sleep(settings.request_delay_seconds)
                await asyncio.sleep(settings.request_delay_seconds)
        # 任一上下文失败时不覆盖旧库存，避免把暂时无法访问的资产误标为不可出售。
        if not errors:
            self.store.replace_inventory(assets)
        self.store.audit("inventory.sync", status.steam_id, {"count": len(assets), "errors": errors})
        return SyncResult(
            inventory_count=len(assets),
            marketable_count=sum(item.marketable for item in assets),
            errors=errors,
        )

    async def update_price_history(
        self,
        appid: int,
        market_hash_name: str,
        currency: Currency,
        *,
        context: Any | None = None,
    ) -> int:
        if context is None:
            async with self.session.browser_context() as owned_context:
                return await self.update_price_history(
                    appid, market_hash_name, currency, context=owned_context
                )
        page = await context.new_page()
        try:
            payload = await self._navigate_json_with_retry(
                page,
                "https://steamcommunity.com/market/pricehistory/",
                params={
                    "appid": appid,
                    "market_hash_name": market_hash_name,
                    "currency": CURRENCY_IDS[currency],
                },
            )
        finally:
            await page.close()
        if not payload.get("success"):
            raise RuntimeError("Steam 未返回有效价格历史")
        currency_marker = f"{payload.get('price_prefix', '')}{payload.get('price_suffix', '')}"
        known_markers = set().union(*CURRENCY_SYMBOLS.values())
        if any(marker in currency_marker for marker in known_markers) and not any(
            marker in currency_marker for marker in CURRENCY_SYMBOLS[currency]
        ):
            raise RuntimeError(
                f"Steam 钱包返回的币种与 {currency.value} 模式不一致，请切换正确币种"
            )
        rows = parse_price_history(payload)
        if not rows:
            raise RuntimeError("Steam 返回成功，但最近30天价格数据为空或格式无法识别")
        self.store.save_prices(appid, market_hash_name, currency.value, rows)
        return len(rows)

    async def sync_all_prices(self, currency: Currency) -> SyncResult:
        items = {
            (int(item["appid"]), str(item["market_hash_name"]))
            for item in self.store.inventory(marketable_only=True)
        }
        updated = 0
        errors: list[str] = []
        async with self.session.browser_context() as context:
            for appid, market_hash_name in sorted(items):
                try:
                    await self.update_price_history(
                        appid, market_hash_name, currency, context=context
                    )
                    updated += 1
                except RuntimeError as exc:
                    errors.append(f"{market_hash_name}: {exc}")
                await asyncio.sleep(settings.request_delay_seconds)
        return SyncResult(price_items_updated=updated, errors=errors)

    async def current_lowest_price(
        self, appid: int, market_hash_name: str, currency: Currency
    ) -> int | None:
        async with self.session.browser_context() as context:
            response = await context.request.get(
                "https://steamcommunity.com/market/priceoverview/",
                params={
                    "appid": appid,
                    "market_hash_name": market_hash_name,
                    "currency": CURRENCY_IDS[currency],
                    "country": "CN" if currency is Currency.CNY else "IN",
                },
            )
            payload = await response.json()
        value = str(payload.get("lowest_price", ""))
        cleaned = "".join(character for character in value if character.isdigit() or character in ".,")
        if not cleaned:
            return None
        if "," in cleaned and "." not in cleaned:
            cleaned = cleaned.replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
        try:
            return round(float(cleaned) * 100)
        except ValueError:
            return None

    async def create_listing(
        self, appid: int, contextid: str, assetid: str, seller_price_minor: int
    ) -> dict[str, Any]:
        if settings.dry_run or not settings.allow_market_writes:
            raise PermissionError("真实市场操作未启用")
        async with self.session.browser_context() as context:
            cookies = await context.cookies(["https://steamcommunity.com"])
            session_id = next(
                (cookie["value"] for cookie in cookies if cookie["name"] == "sessionid"), None
            )
            if not session_id:
                raise RuntimeError("Steam 会话缺少 sessionid")
            response = await context.request.post(
                "https://steamcommunity.com/market/sellitem/",
                form={
                    "sessionid": session_id,
                    "appid": str(appid),
                    "contextid": contextid,
                    "assetid": assetid,
                    "amount": "1",
                    "price": str(seller_price_minor),
                },
                headers={"Referer": f"https://steamcommunity.com/profiles/{self.session.status().steam_id}/inventory/"},
            )
            payload = await response.json()
        if not payload.get("success"):
            raise RuntimeError(f"Steam 拒绝上架：{payload}")
        return payload

    async def cancel_listing(self, steam_listing_id: str) -> None:
        if settings.dry_run or not settings.allow_market_writes:
            raise PermissionError("真实市场操作未启用")
        async with self.session.browser_context() as context:
            cookies = await context.cookies(["https://steamcommunity.com"])
            session_id = next(
                (cookie["value"] for cookie in cookies if cookie["name"] == "sessionid"), None
            )
            response = await context.request.post(
                f"https://steamcommunity.com/market/removelisting/{steam_listing_id}",
                form={"sessionid": session_id or ""},
                headers={"Referer": "https://steamcommunity.com/market/"},
            )
        if not response.ok:
            raise RuntimeError(f"Steam 撤单失败：HTTP {response.status}")

    async def active_listings(self) -> list[dict[str, object]]:
        """读取我的市场挂单页面，返回可用于状态匹配的最小字段集合。"""
        async with self.session.browser_context() as context:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(
                "https://steamcommunity.com/market/",
                wait_until="domcontentloaded",
                timeout=45_000,
            )
            return await page.evaluate(
                """
                () => Array.from(document.querySelectorAll('[id^="mylisting_"]')).map(row => {
                  const id = row.id.replace('mylisting_', '');
                  const name = row.querySelector('.market_listing_item_name')?.textContent?.trim() || '';
                  const price = row.querySelector('.market_listing_price')?.textContent?.trim() || '';
                  return { listing_id: id, market_hash_name: name, display_price: price };
                })
                """
            )

    async def recent_sales(self) -> set[str]:
        """读取最近市场历史，用于区分售出与手动撤单。"""
        async with self.session.browser_context() as context:
            response = await context.request.get(
                "https://steamcommunity.com/market/myhistory/render/",
                params={"query": "", "start": 0, "count": 100},
                timeout=45_000,
            )
            if not response.ok:
                return set()
            payload = await response.json()
            html = str(payload.get("results_html", ""))
            page = await context.new_page()
            await page.set_content(f"<main>{html}</main>")
            names = await page.evaluate(
                """
                () => Array.from(document.querySelectorAll('.market_listing_row'))
                  .filter(row => {
                    const marker = row.querySelector('.market_listing_gainorloss');
                    return marker && marker.textContent.includes('+');
                  })
                  .map(row => row.querySelector('.market_listing_item_name')?.textContent?.trim())
                  .filter(Boolean)
                """
            )
            await page.close()
            return set(names)


steam_market_service = SteamMarketService()
