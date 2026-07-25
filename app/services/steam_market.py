import asyncio
import json
import logging
import secrets
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
logger = logging.getLogger(__name__)
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


def enrich_active_listing_rows(
    payload: dict[str, Any], html_rows: list[dict[str, object]]
) -> list[dict[str, object]]:
    """用接口中的结构化 listinginfo/assets 补全 HTML 行。"""
    assets: dict[tuple[str, str, str], dict[str, Any]] = {}
    for appid, contexts in (payload.get("assets") or {}).items():
        if not isinstance(contexts, dict):
            continue
        for contextid, context_assets in contexts.items():
            if not isinstance(context_assets, dict):
                continue
            for assetid, asset in context_assets.items():
                if isinstance(asset, dict):
                    assets[(str(appid), str(contextid), str(assetid))] = asset

    structured: dict[str, dict[str, Any]] = {}
    raw_listinginfo = payload.get("listinginfo") or {}
    listing_values = (
        raw_listinginfo.values()
        if isinstance(raw_listinginfo, dict)
        else raw_listinginfo
        if isinstance(raw_listinginfo, list)
        else []
    )
    for info in listing_values:
        if isinstance(info, dict) and info.get("listingid"):
            structured[str(info["listingid"])] = info

    result: list[dict[str, object]] = []
    seen: set[str] = set()
    for raw_row in html_rows:
        listing_id = str(raw_row.get("listing_id") or "")
        if not listing_id.isdigit() or listing_id in seen:
            continue
        seen.add(listing_id)
        info = structured.get(listing_id, {})
        asset_ref = info.get("asset") if isinstance(info.get("asset"), dict) else {}
        appid = str(asset_ref.get("appid") or raw_row.get("appid") or "0")
        contextid = str(asset_ref.get("contextid") or raw_row.get("contextid") or "")
        assetid = str(
            asset_ref.get("id")
            or asset_ref.get("assetid")
            or raw_row.get("assetid")
            or ""
        )
        asset = assets.get((appid, contextid, assetid), {})
        name = str(
            asset.get("market_hash_name")
            or asset.get("name")
            or raw_row.get("market_hash_name")
            or ""
        )
        converted_price = int(info.get("converted_price") or 0)
        converted_fee = int(info.get("converted_fee") or 0)
        listed_at_value = info.get("time_created")
        if isinstance(listed_at_value, (int, float)) and listed_at_value > 0:
            listed_at = datetime.fromtimestamp(listed_at_value, UTC).isoformat()
        else:
            listed_at = str(
                raw_row.get("listed_at")
                or raw_row.get("listed_at_text")
                or ""
            )
        result.append(
            {
                **raw_row,
                "listing_id": listing_id,
                "appid": int(appid or 0),
                "contextid": contextid,
                "assetid": assetid,
                "market_hash_name": name,
                "buyer_price_minor": converted_price + converted_fee,
                "listed_at": listed_at,
            }
        )
    return result


class SteamMarketService:
    def __init__(
        self,
        session: SteamSessionService | None = None,
        store: Database | None = None,
    ) -> None:
        self.session = session or steam_session_service
        self.store = store or database

    async def _ensure_session_id(self, context: Any) -> str:
        """返回 Steam CSRF token；缺失时写入与表单同值的新 Cookie。"""
        cookies = await context.cookies()
        session_id = next(
            (
                str(cookie["value"])
                for cookie in cookies
                if cookie.get("name") == "sessionid"
                and "steamcommunity.com" in str(cookie.get("domain", ""))
            ),
            None,
        )
        if session_id:
            return session_id
        session_id = secrets.token_hex(12)
        await context.add_cookies(
            [
                {
                    "name": "sessionid",
                    "value": session_id,
                    "domain": "steamcommunity.com",
                    "path": "/",
                    "secure": True,
                    "sameSite": "Lax",
                }
            ]
        )
        return session_id

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

    async def _browser_post_form(
        self,
        context: Any,
        url: str,
        form: dict[str, str],
    ) -> dict[str, Any]:
        """在 Steam 页面内提交表单，确保请求使用 Chromium 的代理链路。"""
        page = next(
            (
                candidate
                for candidate in context.pages
                if candidate.url.startswith("https://steamcommunity.com/")
            ),
            None,
        ) or await context.new_page()
        if not page.url.startswith("https://steamcommunity.com/"):
            navigation_error: PlaywrightError | None = None
            for attempt in range(settings.request_retries + 1):
                try:
                    await page.goto(
                        "https://steamcommunity.com/market/",
                        wait_until="domcontentloaded",
                        timeout=settings.request_timeout_seconds * 1000,
                    )
                    navigation_error = None
                    break
                except PlaywrightError as exc:
                    navigation_error = exc
                    if attempt < settings.request_retries:
                        await asyncio.sleep(2**attempt)
            if navigation_error is not None:
                raise RuntimeError(
                    f"Steam 市场页面无法打开，已重试 {settings.request_retries} 次；"
                    "请检查 Chromium 使用的 VPN/加速器"
                ) from navigation_error
        last_error = "浏览器网络请求失败"
        for attempt in range(settings.request_retries + 1):
            try:
                result = await page.evaluate(
                    """
                    async ({url, form, timeoutMs}) => {
                      const controller = new AbortController();
                      const timer = setTimeout(() => controller.abort(), timeoutMs);
                      try {
                        const response = await fetch(url, {
                          method: 'POST',
                          credentials: 'include',
                          headers: {
                            'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                            'X-Requested-With': 'XMLHttpRequest'
                          },
                          body: new URLSearchParams(form).toString(),
                          signal: controller.signal
                        });
                        const text = await response.text();
                        let payload = null;
                        try {
                          payload = text ? JSON.parse(text) : {};
                        } catch {
                          return {
                            ok: false,
                            status: response.status,
                            error: 'Steam 返回了无法识别的数据'
                          };
                        }
                        return {
                          ok: response.ok,
                          status: response.status,
                          payload,
                          error: response.ok ? null : `HTTP ${response.status}`
                        };
                      } catch (error) {
                        return {
                          ok: false,
                          status: 0,
                          error: error?.name === 'AbortError'
                            ? '请求超时'
                            : '浏览器网络请求失败'
                        };
                      } finally {
                        clearTimeout(timer);
                      }
                    }
                    """,
                    {
                        "url": url,
                        "form": form,
                        "timeoutMs": settings.request_timeout_seconds * 1000,
                    },
                )
            except PlaywrightError:
                result = {"ok": False, "status": 0, "error": "浏览器执行请求失败"}
            if result.get("ok"):
                return dict(result.get("payload") or {})
            last_error = str(result.get("error") or "Steam 请求失败")
            status = int(result.get("status") or 0)
            if attempt >= settings.request_retries or (0 < status < 500 and status != 429):
                break
            await asyncio.sleep(2**attempt)
        raise RuntimeError(
            f"{last_error}，已重试 {settings.request_retries} 次；"
            "请确认 Steam 市场页面可在登录浏览器中打开"
        )

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

    async def sync_selected_prices(
        self, assetids: list[str], currency: Currency
    ) -> SyncResult:
        selected_ids = {str(assetid) for assetid in assetids}
        items = {
            (int(item["appid"]), str(item["market_hash_name"]))
            for item in self.store.inventory(marketable_only=True)
            if str(item["assetid"]) in selected_ids
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
                    logger.warning(
                        "Steam 选中饰品30天价格同步失败：appid=%s item=%s "
                        "currency=%s error=%s",
                        appid,
                        market_hash_name,
                        currency.value,
                        exc,
                    )
                    errors.append(f"{market_hash_name}: {exc}")
                await asyncio.sleep(settings.request_delay_seconds)
        return SyncResult(price_items_updated=updated, errors=errors)

    async def current_lowest_price(
        self, appid: int, market_hash_name: str, currency: Currency
    ) -> int | None:
        async with self.session.browser_context() as context:
            page = context.pages[0] if context.pages else await context.new_page()
            try:
                payload = await self._navigate_json_with_retry(
                    page,
                    "https://steamcommunity.com/market/priceoverview/",
                    params={
                        "appid": appid,
                        "market_hash_name": market_hash_name,
                        "currency": CURRENCY_IDS[currency],
                        "country": "CN" if currency is Currency.CNY else "IN",
                    },
                )
            except RuntimeError as exc:
                logger.warning(
                    "Steam 实时最低价获取失败：appid=%s item=%s currency=%s "
                    "retries=%s fallback=30天历史价格 error=%s",
                    appid,
                    market_hash_name,
                    currency.value,
                    settings.request_retries,
                    exc,
                )
                return None
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
            session_id = await self._ensure_session_id(context)
            payload = await self._browser_post_form(
                context,
                "https://steamcommunity.com/market/sellitem/",
                {
                    "sessionid": session_id,
                    "appid": str(appid),
                    "contextid": contextid,
                    "assetid": assetid,
                    "amount": "1",
                    "price": str(seller_price_minor),
                },
            )
        if not payload.get("success"):
            reason = payload.get("message") or payload.get("error") or "未提供原因"
            raise RuntimeError(f"Steam 拒绝上架：{reason}")
        return payload

    async def cancel_listing(self, steam_listing_id: str) -> None:
        if settings.dry_run or not settings.allow_market_writes:
            raise PermissionError("真实市场操作未启用")
        async with self.session.browser_context() as context:
            session_id = await self._ensure_session_id(context)
            payload = await self._browser_post_form(
                context,
                f"https://steamcommunity.com/market/removelisting/{steam_listing_id}",
                {"sessionid": session_id},
            )
        if payload.get("success") is False or payload.get("success") == 0:
            raise RuntimeError("Steam 拒绝撤单")

    async def active_listings(self) -> list[dict[str, object]]:
        """分页读取 Steam 我的在售，包含程序启动前创建的挂单。"""
        listings: list[dict[str, object]] = []
        async with self.session.browser_context() as context:
            api_page = await context.new_page()
            parser_page = await context.new_page()
            try:
                start = 0
                count = 100
                while True:
                    payload = await self._navigate_json_with_retry(
                        api_page,
                        "https://steamcommunity.com/market/mylistings/render/",
                        params={"query": "", "start": start, "count": count},
                    )
                    html = str(payload.get("results_html", ""))
                    await parser_page.set_content(f"<main>{html}</main>")
                    page_rows = await parser_page.evaluate(
                        """
                        () => Array.from(
                          document.querySelectorAll('.market_listing_row[id^="mylisting_"]')
                        ).map(row => {
                          const listingId = (row.id.match(/^mylisting_(\\d+)$/) || [])[1] || '';
                          const name = row.querySelector(
                            `#mylisting_${listingId}_name`
                          )?.textContent?.trim() || row.querySelector(
                            '.market_listing_item_name_link'
                          )?.textContent?.trim() || '';
                          const price = row.querySelector(
                            '.market_listing_price'
                          )?.textContent?.trim() || '';
                          const action = Array.from(
                            row.querySelectorAll('[href], [onclick]')
                          ).map(node =>
                            `${node.getAttribute('href') || ''} ${
                              node.getAttribute('onclick') || ''
                            }`
                          ).find(text =>
                            text.includes('MarketListing') && text.includes(listingId)
                          ) || '';
                          const args = action.match(
                            /\\(\\s*'[^']*'\\s*,\\s*'(\\d+)'\\s*,\\s*(\\d+)\\s*,\\s*'([^']+)'\\s*,\\s*'([^']+)'/
                          );
                          const marketLink = row.querySelector(
                            'a.market_listing_item_name_link'
                          )?.href || '';
                          const appMatch = marketLink.match(/\\/market\\/listings\\/(\\d+)\\//);
                          const listedAtText = row.querySelector(
                            '.market_listing_listed_date'
                          )?.textContent?.trim() || '';
                          return {
                            listing_id: listingId,
                            market_hash_name: name,
                            display_price: price,
                            appid: args ? Number(args[2]) : (
                              appMatch ? Number(appMatch[1]) : 0
                            ),
                            contextid: args ? args[3] : '',
                            assetid: args ? args[4] : '',
                            listed_at_text: listedAtText,
                            listed_at: listedAtText
                          };
                        })
                        """
                    )
                    listings.extend(enrich_active_listing_rows(payload, page_rows))
                    total = int(payload.get("total_count", len(listings)))
                    start += len(page_rows)
                    if not page_rows or start >= total:
                        break
                    await asyncio.sleep(settings.request_delay_seconds)
            finally:
                await parser_page.close()
                await api_page.close()
        return listings

    async def recent_sales(self) -> set[str]:
        """读取最近市场历史，用于区分售出与手动撤单。"""
        async with self.session.browser_context() as context:
            page = context.pages[0] if context.pages else await context.new_page()
            payload = await self._navigate_json_with_retry(
                page,
                "https://steamcommunity.com/market/myhistory/render/",
                params={"query": "", "start": 0, "count": 100},
            )
            html = str(payload.get("results_html", ""))
            parser_page = await context.new_page()
            await parser_page.set_content(f"<main>{html}</main>")
            names = await parser_page.evaluate(
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
            await parser_page.close()
            return set(names)


steam_market_service = SteamMarketService()
