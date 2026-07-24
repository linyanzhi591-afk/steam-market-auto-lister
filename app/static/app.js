const $ = (selector) => document.querySelector(selector);
let currentListings = [];

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

function money(minor) {
  const symbol = $("#currency").value === "CNY" ? "¥" : "₹";
  return `${symbol}${(minor / 100).toFixed(2)}`;
}

function toast(message) {
  $("#toast").textContent = message;
  $("#toast").hidden = false;
  setTimeout(() => { $("#toast").hidden = true; }, 5000);
}

function renderSession(session) {
  $("#session-state").textContent = session.state;
  $("#session-message").textContent = session.message;
  $("#login").hidden = session.state === "logged_in";
  $("#logout").hidden = session.state !== "logged_in";
}

function renderInventory(items) {
  $("#inventory-body").innerHTML = items.length
    ? items.map((item) => `<tr>
        <td>${item.appid}</td><td>${item.market_hash_name}</td>
        <td>${item.amount}</td><td>${item.tradable ? "是" : "否"}</td>
      </tr>`).join("")
    : '<tr><td colspan="4">没有已同步的可出售库存</td></tr>';
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
    const currency = $("#currency").value;
    const [health, session, dashboard, strategies, inventory, listings] = await Promise.all([
      getJSON("/api/health"), getJSON("/api/session"),
      getJSON(`/api/dashboard?currency=${currency}`), getJSON("/api/strategies"),
      getJSON("/api/inventory?marketable_only=true"), getJSON("/api/listings"),
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
    renderInventory(inventory);
    renderListings(listings);
  } catch (error) {
    $("#health").textContent = error.message;
  }
}

async function busy(button, action) {
  button.disabled = true;
  try { await action(); } catch (error) { toast(error.message); } finally { button.disabled = false; }
}

$("#login").addEventListener("click", () => busy($("#login"), async () => {
  renderSession({ state: "logging_in", message: "请在 Steam 官方窗口完成登录" });
  const result = await post("/api/session/login");
  renderSession(result.status);
}));
$("#logout").addEventListener("click", () => busy($("#logout"), async () => {
  renderSession((await post("/api/session/logout")).status);
}));
$("#sync-inventory").addEventListener("click", () => busy($("#sync-inventory"), async () => {
  const result = await post("/api/sync/inventory");
  if (result.errors.length) {
    throw new Error(`库存同步未完成：${result.errors.join("；")}`);
  }
  toast(`已同步 ${result.inventory_count} 件库存，其中 ${result.marketable_count} 件可出售`);
  await load();
}));
$("#sync-prices").addEventListener("click", () => busy($("#sync-prices"), async () => {
  const result = await post(`/api/sync/prices?currency=${$("#currency").value}`);
  toast(`已更新 ${result.price_items_updated} 种饰品的30天价格`);
}));
$("#sync-listings").addEventListener("click", () => busy($("#sync-listings"), async () => {
  const result = await post("/api/sync/listings");
  toast(`已更新 ${result.listings_updated} 条挂单状态`);
  await load();
}));
$("#create-plans").addEventListener("click", () => busy($("#create-plans"), async () => {
  const plans = await post("/api/listings/plan", {
    strategy: $("#plan-strategy").value,
    currency: $("#currency").value,
    minimum_receive_minor: Math.round(Number($("#minimum-price").value) * 100),
    maximum_buyer_price_minor: Math.round(Number($("#maximum-price").value) * 100),
    maximum_items: Number($("#maximum-items").value),
    excluded_names: $("#excluded-names").value.split(",").map((name) => name.trim()).filter(Boolean),
  });
  toast(`已生成 ${plans.length} 条上架计划`);
  await load();
}));
$("#execute-plans").addEventListener("click", () => busy($("#execute-plans"), async () => {
  const ids = currentListings.filter((item) => item.state === "planned").map((item) => item.id);
  if (!ids.length) throw new Error("没有待提交计划");
  const confirmation = window.prompt("输入“我确认执行真实市场操作”以提交所有计划；提交后仍需手机确认");
  if (!confirmation) return;
  await post("/api/listings/execute", { listing_ids: ids, confirmation_text: confirmation });
  await load();
}));
$("#currency").addEventListener("change", load);
load();
