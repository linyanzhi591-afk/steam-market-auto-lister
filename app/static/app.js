const $ = (selector) => document.querySelector(selector);
let currentListings = [];
let groupedInventory = [];

async function request(path, options = {}) {
  const response = await fetch(path, options);
  const contentType = response.headers.get("content-type") || "";
  let data;
  if (contentType.includes("application/json")) {
    data = await response.json();
  } else {
    const text = await response.text();
    data = { detail: text || response.statusText || "服务器未返回错误详情" };
  }
  if (!response.ok) {
    throw new Error(data.detail || `请求失败：${response.status}`);
  }
  return data;
}

const getJSON = (path) => request(path);
const post = (path, body) => request(path, {
  method: "POST",
  headers: body ? { "Content-Type": "application/json" } : {},
  body: body ? JSON.stringify(body) : undefined,
});
const put = (path, body) => request(path, {
  method: "PUT",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});
const remove = (path) => request(path, { method: "DELETE" });

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function money(minor) {
  const symbol = $("#currency").value === "CNY" ? "¥" : "₹";
  return `${symbol}${(minor / 100).toFixed(2)}`;
}

function toast(message) {
  $("#toast").textContent = message;
  $("#toast").hidden = false;
  setTimeout(() => { $("#toast").hidden = true; }, 15000);
}

function renderSession(session) {
  $("#session-state").textContent = session.state;
  $("#session-message").textContent = session.message;
  $("#login").hidden = session.state === "logged_in";
  $("#logout").hidden = session.state !== "logged_in";
}

function renderInventory(items) {
  groupedInventory = Array.from(items.reduce((groups, item) => {
    const key = `${item.appid}\u0000${item.market_hash_name}`;
    const current = groups.get(key);
    if (current) {
      current.amount += Number(item.amount);
      current.assetCount += 1;
      current.assetids.push(String(item.assetid));
      current.tradable = current.tradable && Boolean(item.tradable);
    } else {
      groups.set(key, {
        appid: item.appid,
        market_hash_name: item.market_hash_name,
        amount: Number(item.amount),
        assetCount: 1,
        assetids: [String(item.assetid)],
        tradable: Boolean(item.tradable),
      });
    }
    return groups;
  }, new Map()).values());

  $("#inventory-body").innerHTML = groupedInventory.length
    ? groupedInventory.map((item, index) => `<tr>
        <td><input class="inventory-select" type="checkbox" data-group-index="${index}" aria-label="选择 ${escapeHtml(item.market_hash_name)}" /></td>
        <td>${item.appid}</td><td>${escapeHtml(item.market_hash_name)}</td>
        <td>${item.amount}${item.assetCount > 1 ? `（${item.assetCount} 个独立资产）` : ""}</td>
        <td>${item.tradable ? "是" : "否"}</td>
        <td><button class="secondary blacklist-add" type="button" data-group-index="${index}">加入黑名单</button></td>
      </tr>`).join("")
    : '<tr><td colspan="6">没有已同步的可出售库存</td></tr>';
  $("#select-all-inventory").checked = false;
  $("#select-all-inventory").indeterminate = false;
}

function renderBlacklist(items) {
  $("#blacklist-body").innerHTML = items.length
    ? items.map((item, index) => `<tr>
        <td>${item.appid}</td>
        <td>${escapeHtml(item.market_hash_name)}</td>
        <td><button class="secondary blacklist-remove" type="button" data-blacklist-index="${index}">移出黑名单</button></td>
      </tr>`).join("")
    : '<tr><td colspan="3">黑名单为空</td></tr>';
  $("#blacklist-body").dataset.items = JSON.stringify(items);
}

function renderListings(items) {
  currentListings = items;
  $("#listings-body").innerHTML = items.length
    ? items.map((item) => `<tr>
        <td>${item.state}</td><td>${item.market_hash_name}</td><td>${item.strategy}</td>
        <td>${money(item.seller_price_minor)}</td><td>${money(item.buyer_price_minor)}</td>
      </tr>`).join("")
    : '<tr><td colspan="5">暂无任务</td></tr>';
}

async function load() {
  try {
    const [session, appSettings] = await Promise.all([
      getJSON("/api/session"),
      getJSON("/api/settings"),
    ]);
    const currency = appSettings.currency;
    $("#currency").value = currency;
    const [health, dashboard, strategies, inventory, listings, blacklist] = await Promise.all([
      getJSON("/api/health"),
      getJSON(`/api/dashboard?currency=${currency}`), getJSON("/api/strategies"),
      getJSON("/api/inventory?marketable_only=true"), getJSON("/api/listings"),
      getJSON("/api/blacklist"),
    ]);
    $("#health").textContent = health.status === "ok" ? "本地服务正常" : "服务异常";
    $("#mode-label").textContent = dashboard.dry_run ? "演练模式。" : "真实市场模式已启用。";
    renderSession(session);
    $("#sellable").textContent = dashboard.sellable_items;
    $("#pending").textContent = dashboard.pending_confirmation;
    $("#active").textContent = dashboard.active;
    $("#strategies").innerHTML = strategies
      .map((item) => `<div class="strategy"><strong>${item.name}</strong><small>${item.id}</small></div>`)
      .join("");
    $("#default-strategy").value = appSettings.default_strategy;
    $("#plan-strategy").value = appSettings.default_strategy;
    $("#trend-hours").value = appSettings.trend_hours;
    $("#robust-hours").value = appSettings.robust_median_hours;
    $("#follow-hours").value = appSettings.market_follow_hours;
    $("#fast-hours").value = appSettings.fast_sell_hours;
    renderInventory(inventory);
    renderListings(listings);
    renderBlacklist(blacklist);
  } catch (error) {
    $("#health").textContent = error.message;
  }
}

async function busy(button, action) {
  const originalText = button.textContent;
  button.disabled = true;
  button.textContent = "处理中，请稍候…";
  try {
    await action();
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
    button.textContent = originalText;
  }
}

$("#login").addEventListener("click", () => busy($("#login"), async () => {
  renderSession({ state: "logging_in", message: "请在 Steam 官方窗口完成登录" });
  const result = await post("/api/session/login");
  renderSession(result.status);
  await load();
}));
$("#logout").addEventListener("click", () => busy($("#logout"), async () => {
  renderSession((await post("/api/session/logout")).status);
}));
$("#sync-inventory").addEventListener("click", () => busy($("#sync-inventory"), async () => {
  const status = $("#sync-status");
  status.className = "sync-status";
  status.textContent = "正在连接 Steam 并逐页读取库存，请保持 VPN/加速器连接…";
  try {
    const result = await post("/api/sync/inventory");
    if (result.errors.length) {
      throw new Error(`库存同步未完成：${result.errors.join("；")}`);
    }
    status.className = "sync-status success";
    status.textContent = `同步完成：${result.inventory_count} 件库存，${result.marketable_count} 件可出售`;
    toast(status.textContent);
    await load();
  } catch (error) {
    status.className = "sync-status error";
    status.textContent = error.message;
    throw error;
  }
}));
$("#sync-prices").addEventListener("click", () => busy($("#sync-prices"), async () => {
  const status = $("#sync-status");
  status.className = "sync-status";
  status.textContent = "正在读取每种饰品最近30天价格，请勿关闭页面…";
  try {
    const result = await post(`/api/sync/prices?currency=${$("#currency").value}`);
    if (result.errors.length) {
      throw new Error(`价格同步未完成：${result.errors.join("；")}`);
    }
    status.className = "sync-status success";
    status.textContent = `价格同步完成：已更新 ${result.price_items_updated} 种饰品`;
    toast(status.textContent);
  } catch (error) {
    status.className = "sync-status error";
    status.textContent = error.message;
    throw error;
  }
}));
$("#sync-listings").addEventListener("click", () => busy($("#sync-listings"), async () => {
  const result = await post("/api/sync/listings");
  toast(`已更新 ${result.listings_updated} 条挂单状态`);
  await load();
}));
$("#create-plans").addEventListener("click", () => busy($("#create-plans"), async () => {
  const selectedAssetids = Array.from(document.querySelectorAll(".inventory-select:checked"))
    .flatMap((checkbox) => groupedInventory[Number(checkbox.dataset.groupIndex)].assetids);
  if (!selectedAssetids.length) throw new Error("请先选择至少一项库存");
  const plans = await post("/api/listings/plan", {
    assetids: selectedAssetids,
    strategy: $("#plan-strategy").value,
    currency: $("#currency").value,
    minimum_receive_minor: Math.round(Number($("#minimum-price").value) * 100),
    maximum_buyer_price_minor: Math.round(Number($("#maximum-price").value) * 100),
    maximum_items: Number($("#maximum-items").value),
  });
  toast(`已生成 ${plans.length} 条上架计划`);
  await load();
}));
$("#select-all-inventory").addEventListener("change", (event) => {
  document.querySelectorAll(".inventory-select").forEach((checkbox) => {
    checkbox.checked = event.target.checked;
  });
});
$("#inventory-body").addEventListener("change", () => {
  const checkboxes = Array.from(document.querySelectorAll(".inventory-select"));
  const checked = checkboxes.filter((checkbox) => checkbox.checked).length;
  $("#select-all-inventory").checked = checkboxes.length > 0 && checked === checkboxes.length;
  $("#select-all-inventory").indeterminate = checked > 0 && checked < checkboxes.length;
});
$("#inventory-body").addEventListener("click", async (event) => {
  const button = event.target.closest(".blacklist-add");
  if (!button) return;
  await busy(button, async () => {
    const item = groupedInventory[Number(button.dataset.groupIndex)];
    await post("/api/blacklist", {
      appid: item.appid,
      market_hash_name: item.market_hash_name,
    });
    toast(`${item.market_hash_name} 已加入黑名单`);
    await load();
  });
});
$("#blacklist-body").addEventListener("click", async (event) => {
  const button = event.target.closest(".blacklist-remove");
  if (!button) return;
  await busy(button, async () => {
    const items = JSON.parse($("#blacklist-body").dataset.items || "[]");
    const item = items[Number(button.dataset.blacklistIndex)];
    const query = new URLSearchParams({
      appid: String(item.appid),
      market_hash_name: item.market_hash_name,
    });
    await remove(`/api/blacklist?${query}`);
    toast(`${item.market_hash_name} 已移出黑名单`);
    await load();
  });
});
$("#save-settings").addEventListener("click", () => busy($("#save-settings"), async () => {
  const saved = await put("/api/settings", {
    currency: $("#currency").value,
    default_strategy: $("#default-strategy").value,
    trend_hours: Number($("#trend-hours").value),
    robust_median_hours: Number($("#robust-hours").value),
    market_follow_hours: Number($("#follow-hours").value),
    fast_sell_hours: Number($("#fast-hours").value),
  });
  $("#plan-strategy").value = saved.default_strategy;
  toast("设置已保存");
}));
document.querySelectorAll(".view-tab").forEach((button) => {
  button.addEventListener("click", () => {
    const view = button.dataset.view;
    document.querySelectorAll(".view-main").forEach((element) => {
      element.hidden = view !== "main";
    });
    document.querySelectorAll(".view-settings").forEach((element) => {
      element.hidden = view !== "settings";
    });
    document.querySelectorAll(".view-tab").forEach((tab) => {
      tab.classList.toggle("active", tab === button);
      tab.classList.toggle("secondary", tab !== button);
    });
  });
});
$("#execute-plans").addEventListener("click", () => busy($("#execute-plans"), async () => {
  const ids = currentListings.filter((item) => item.state === "planned").map((item) => item.id);
  if (!ids.length) throw new Error("没有待提交计划");
  const confirmation = window.prompt("输入“我确认执行真实市场操作”以提交所有计划；提交后仍需手机确认");
  if (!confirmation) return;
  await post("/api/listings/execute", { listing_ids: ids, confirmation_text: confirmation });
  await load();
}));
load();
