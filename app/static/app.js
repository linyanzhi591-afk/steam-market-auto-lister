const $ = (selector) => document.querySelector(selector);
let currentListings = [];
let groupedInventory = [];
let groupedActiveListings = [];
let strategyProfiles = [];
let editingStrategy = null;
let currentAppSettings = null;

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

function displaySteamTime(value) {
  if (!value) return "Steam 未提供";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? escapeHtml(value) : parsed.toLocaleString();
}

function moneyRange(values) {
  const minimum = Math.min(...values);
  const maximum = Math.max(...values);
  return minimum === maximum ? money(minimum) : `${money(minimum)} ～ ${money(maximum)}`;
}

function earliestTime(values) {
  const available = values.filter(Boolean);
  if (!available.length) return null;
  const parseable = available
    .map((value) => ({ value, timestamp: new Date(value).getTime() }))
    .filter((item) => !Number.isNaN(item.timestamp))
    .sort((left, right) => left.timestamp - right.timestamp);
  return parseable[0]?.value || available[0];
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

const pricingSourceLabels = {
  robust_median: "时间加权稳健价",
  market_follow: "市场跟随价",
  trend: "趋势预测价",
  fast_sell: "快速出售价",
};
const listingPriceSourceLabels = {
  strategy: "当前策略计算",
  age_reprice_reference: "同款时间调价参考",
  age_reference_confirmed: "已确认同款参考价",
  strategy_confirmed: "已确认策略价",
  custom_confirmed: "用户自定义价",
  strategy_reference_expired: "参考失效，改用策略价",
};

function defaultStage() {
  return {
    name: "新阶段",
    pricing_source: "robust_median",
    adjustment_percent: 0,
    adjustment_fixed_minor: 0,
    absolute_floor_minor: 1,
    median_floor_percent: 0,
    history_window_days: 30,
    time_half_life_days: 7,
    recent_window_days: 3,
    trend_window_days: 7,
    trend_half_life_days: 3,
    forecast_hours: 6,
    recent_floor_percent: 90,
    long_floor_percent: 85,
    maximum_drop_percent: null,
    minimum_price_points: 24,
    duration_hours: 24,
    action_after_timeout: "next",
  };
}

function renderStrategySelectors() {
  const options = strategyProfiles.map((profile) =>
    `<option value="${profile.id}">${escapeHtml(profile.name)}${profile.is_default ? "（默认）" : ""}</option>`
  ).join("");
  $("#strategy-profile-select").innerHTML = options;
  $("#plan-strategy").innerHTML = options;
  $("#reprice-strategy").innerHTML = options;
  const selected = editingStrategy || strategyProfiles.find((profile) => profile.is_default) || strategyProfiles[0];
  if (selected) {
    $("#strategy-profile-select").value = String(selected.id || "");
    $("#plan-strategy").value = String(selected.id || "");
    $("#reprice-strategy").value = String(selected.id || "");
  }
}

function renderStrategyEditor(profile) {
  editingStrategy = structuredClone(profile);
  $("#strategy-profile-name").value = editingStrategy.name;
  $("#strategy-is-default").checked = editingStrategy.is_default;
  $("#strategy-stages").innerHTML = editingStrategy.stages.map((stage, index) => `
    <article class="stage-card" data-stage-index="${index}">
      <div class="stage-card-head">
        <strong>阶段 ${index + 1}</strong>
        <div class="actions">
          <button class="secondary stage-up" type="button">上移</button>
          <button class="secondary stage-down" type="button">下移</button>
          <button class="danger stage-delete" type="button">删除</button>
        </div>
      </div>
      <div class="stage-fields">
        <label>阶段名称<input data-field="name" value="${escapeHtml(stage.name)}" /></label>
        <label>定价来源<select data-field="pricing_source">
          ${Object.entries(pricingSourceLabels).map(([value, label]) =>
            `<option value="${value}" ${stage.pricing_source === value ? "selected" : ""}>${label}</option>`
          ).join("")}
        </select></label>
        <label>百分比调整<input data-field="adjustment_percent" type="number" step="0.1" min="-90" max="500" value="${stage.adjustment_percent}" /></label>
        <label>固定金额调整<input data-field="adjustment_fixed" type="number" step="0.01" value="${stage.adjustment_fixed_minor / 100}" /></label>
        <label>绝对上架底价<input data-field="absolute_floor" type="number" min="0.01" step="0.01" value="${stage.absolute_floor_minor / 100}" /></label>
        <label>中位价底线比例<input data-field="median_floor_percent" type="number" min="0" max="300" step="0.1" value="${stage.median_floor_percent}" /></label>
        <label>历史窗口（天）<input data-field="history_window_days" type="number" min="3" max="30" value="${stage.history_window_days}" /></label>
        <label>长期权重半衰期（天）<input data-field="time_half_life_days" type="number" min="0.5" max="30" step="0.5" value="${stage.time_half_life_days}" /></label>
        <label>近期窗口（天）<input data-field="recent_window_days" type="number" min="1" max="14" value="${stage.recent_window_days}" /></label>
        <label>趋势窗口（天）<input data-field="trend_window_days" type="number" min="2" max="30" value="${stage.trend_window_days}" /></label>
        <label>趋势权重半衰期（天）<input data-field="trend_half_life_days" type="number" min="0.5" max="30" step="0.5" value="${stage.trend_half_life_days}" /></label>
        <label>趋势预测（小时）<input data-field="forecast_hours" type="number" min="0" max="48" value="${stage.forecast_hours}" /></label>
        <label>近期价格底线（%）<input data-field="recent_floor_percent" type="number" min="0" max="200" step="0.1" value="${stage.recent_floor_percent}" /></label>
        <label>长期价格底线（%）<input data-field="long_floor_percent" type="number" min="0" max="200" step="0.1" value="${stage.long_floor_percent}" /></label>
        <label>单次最大降幅（%）<input data-field="maximum_drop_percent" type="number" min="0" max="100" step="0.1" value="${stage.maximum_drop_percent ?? ""}" placeholder="不限制" /></label>
        <label>最低有效价格点<input data-field="minimum_price_points" type="number" min="1" max="1000" value="${stage.minimum_price_points}" /></label>
        <label>持续时间（小时）<input data-field="duration_hours" type="number" min="1" max="720" value="${stage.duration_hours}" /></label>
        <label>超时动作<select data-field="action_after_timeout">
          <option value="next" ${stage.action_after_timeout === "next" ? "selected" : ""}>进入下一阶段</option>
          <option value="pause" ${stage.action_after_timeout === "pause" ? "selected" : ""}>暂停并人工处理</option>
        </select></label>
      </div>
    </article>
  `).join("");
}

function collectStrategyEditor() {
  const stages = Array.from(document.querySelectorAll(".stage-card")).map((card) => ({
    name: card.querySelector('[data-field="name"]').value.trim(),
    pricing_source: card.querySelector('[data-field="pricing_source"]').value,
    adjustment_percent: Number(card.querySelector('[data-field="adjustment_percent"]').value),
    adjustment_fixed_minor: Math.round(Number(card.querySelector('[data-field="adjustment_fixed"]').value) * 100),
    absolute_floor_minor: Math.round(Number(card.querySelector('[data-field="absolute_floor"]').value) * 100),
    median_floor_percent: Number(card.querySelector('[data-field="median_floor_percent"]').value),
    history_window_days: Number(card.querySelector('[data-field="history_window_days"]').value),
    time_half_life_days: Number(card.querySelector('[data-field="time_half_life_days"]').value),
    recent_window_days: Number(card.querySelector('[data-field="recent_window_days"]').value),
    trend_window_days: Number(card.querySelector('[data-field="trend_window_days"]').value),
    trend_half_life_days: Number(card.querySelector('[data-field="trend_half_life_days"]').value),
    forecast_hours: Number(card.querySelector('[data-field="forecast_hours"]').value),
    recent_floor_percent: Number(card.querySelector('[data-field="recent_floor_percent"]').value),
    long_floor_percent: Number(card.querySelector('[data-field="long_floor_percent"]').value),
    maximum_drop_percent: card.querySelector('[data-field="maximum_drop_percent"]').value === ""
      ? null
      : Number(card.querySelector('[data-field="maximum_drop_percent"]').value),
    minimum_price_points: Number(card.querySelector('[data-field="minimum_price_points"]').value),
    duration_hours: Number(card.querySelector('[data-field="duration_hours"]').value),
    action_after_timeout: card.querySelector('[data-field="action_after_timeout"]').value,
  }));
  if (!stages.length) throw new Error("策略至少需要一个阶段");
  return {
    name: $("#strategy-profile-name").value.trim(),
    is_default: $("#strategy-is-default").checked,
    stages,
  };
}

function renderListings(items, blacklist) {
  currentListings = items;
  const blacklistKeys = new Set(
    blacklist.map((item) => `${item.appid}\u0000${item.market_hash_name}`)
  );
  const priceReviews = items.filter((item) => item.state === "price_review");
  const newListings = items.filter((item) =>
    ["planned", "pending_confirmation", "failed"].includes(item.state)
  );
  const activeListings = items.filter((item) => item.state === "active");
  groupedActiveListings = Array.from(activeListings.reduce((groups, item) => {
    const key = `${item.appid}\u0000${item.market_hash_name}`;
    const current = groups.get(key);
    if (current) {
      current.listingIds.push(item.id);
      current.strategies.add(item.strategy);
      current.buyerPrices.push(item.buyer_price_minor);
      current.steamListedTimes.push(item.steam_listed_at);
      current.nextActionTimes.push(item.next_action_at);
      if (item.error_message) current.errors.add(item.error_message);
    } else {
      groups.set(key, {
        appid: item.appid,
        market_hash_name: item.market_hash_name,
        listingIds: [item.id],
        strategies: new Set([item.strategy]),
        buyerPrices: [item.buyer_price_minor],
        steamListedTimes: [item.steam_listed_at],
        nextActionTimes: [item.next_action_at],
        errors: new Set(item.error_message ? [item.error_message] : []),
        blacklisted: blacklistKeys.has(key),
      });
    }
    return groups;
  }, new Map()).values());
  $("#new-listings-body").innerHTML = newListings.length
    ? newListings.map((item) => `<tr>
        <td><input class="plan-select" type="checkbox" data-listing-id="${item.id}"
          ${["planned", "failed"].includes(item.state) ? "" : "disabled"}
          aria-label="选择 ${escapeHtml(item.market_hash_name)}" /></td>
        <td>${item.state}</td><td>${escapeHtml(item.market_hash_name)}</td><td>${item.strategy}</td>
        <td>${escapeHtml(listingPriceSourceLabels[item.price_source] || item.price_source)}</td>
        <td>${money(item.buyer_price_minor)}</td>
        <td class="${item.error_message ? "error-text" : ""}">${escapeHtml(
          item.error_message || (item.state === "pending_confirmation" ? "等待 Steam 手机确认" : "")
        )}</td>
      </tr>`).join("")
    : '<tr><td colspan="7">暂无新上架任务</td></tr>';
  $("#select-all-plans").checked = false;
  $("#select-all-plans").indeterminate = false;
  $("#price-review-body").innerHTML = priceReviews.length
    ? priceReviews.map((item) => `<tr>
        <td>${escapeHtml(item.market_hash_name)}</td>
        <td>${money(item.strategy_buyer_price_minor || 0)}</td>
        <td>${money(item.buyer_price_minor)}</td>
        <td class="error-text">${Number(item.price_difference_percent || 0).toFixed(2)}%</td>
        <td><div class="actions">
          <button class="review-choice" data-listing-id="${item.id}" data-choice="reference" type="button">使用参考价</button>
          <button class="review-choice secondary" data-listing-id="${item.id}" data-choice="strategy" type="button">使用策略价</button>
          <button class="review-choice secondary" data-listing-id="${item.id}" data-choice="custom" type="button">自定义</button>
          <button class="review-choice danger" data-listing-id="${item.id}" data-choice="skip" type="button">暂不处理</button>
        </div></td>
      </tr>`).join("")
    : '<tr><td colspan="5">没有价格异常任务</td></tr>';
  $("#active-listings-body").innerHTML = groupedActiveListings.length
    ? groupedActiveListings.map((item, index) => `<tr>
        <td><input class="active-select" type="checkbox" data-group-index="${index}"
          ${item.blacklisted ? "disabled" : ""}
          aria-label="选择 ${escapeHtml(item.market_hash_name)}" /></td>
        <td>${escapeHtml(item.market_hash_name)}</td>
        <td>${item.listingIds.length}</td>
        <td>${item.strategies.size === 1 ? [...item.strategies][0] : "多个策略"}</td>
        <td>${moneyRange(item.buyerPrices)}</td>
        <td>${displaySteamTime(earliestTime(item.steamListedTimes))}</td>
        <td>${earliestTime(item.nextActionTimes)
          ? displaySteamTime(earliestTime(item.nextActionTimes))
          : "未设置"}</td>
        <td class="${item.errors.size ? "error-text" : ""}">${escapeHtml([...item.errors].join("；"))}</td>
        <td><button class="secondary active-blacklist-toggle" type="button" data-group-index="${index}">
          ${item.blacklisted ? "移出黑名单" : "加入黑名单"}
        </button></td>
      </tr>`).join("")
    : '<tr><td colspan="9">当前没有已同步的在售挂单</td></tr>';
  $("#select-all-active").checked = false;
  $("#select-all-active").indeterminate = false;
}

async function load() {
  try {
    const [session, appSettings] = await Promise.all([
      getJSON("/api/session"),
      getJSON("/api/settings"),
    ]);
    const currency = appSettings.currency;
    $("#currency").value = currency;
    const [health, dashboard, inventory, listings, blacklist, profiles] = await Promise.all([
      getJSON("/api/health"),
      getJSON(`/api/dashboard?currency=${currency}`),
      getJSON("/api/inventory?marketable_only=true"), getJSON("/api/listings"),
      getJSON("/api/blacklist"),
      getJSON("/api/strategy-profiles"),
    ]);
    $("#health").textContent = health.status === "ok" ? "本地服务正常" : "服务异常";
    renderSession(session);
    $("#sellable").textContent = dashboard.sellable_items;
    $("#pending").textContent = dashboard.pending_confirmation;
    $("#active").textContent = dashboard.active;
    currentAppSettings = appSettings;
    $("#inventory-pressure-enabled").checked = appSettings.inventory_pressure_enabled;
    $("#inventory-pressure-threshold").value = appSettings.inventory_pressure_threshold;
    strategyProfiles = profiles;
    const preferred = strategyProfiles.find((profile) => profile.is_default) || strategyProfiles[0];
    if (!editingStrategy || !strategyProfiles.some((profile) => profile.id === editingStrategy.id)) {
      editingStrategy = preferred;
    }
    renderStrategySelectors();
    if (editingStrategy) renderStrategyEditor(editingStrategy);
    renderInventory(inventory);
    renderListings(listings, blacklist);
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
$("#shutdown").addEventListener("click", () => busy($("#shutdown"), async () => {
  if (!window.confirm("确定退出程序？Steam 登录状态会保留，下次启动可自动登录。")) return;
  await post("/api/shutdown");
  document.body.innerHTML = `
    <main class="shell shutdown-screen">
      <h1>程序已退出</h1>
      <p class="subtle">现在可以关闭此页面。</p>
    </main>
  `;
  window.close();
}));
$("#sync-data").addEventListener("click", () => busy($("#sync-data"), async () => {
  const status = $("#sync-status");
  status.className = "sync-status";
  status.textContent = "正在同步库存、待手机确认和当前在售，请保持 VPN/加速器连接…";
  try {
    const result = await post("/api/sync/data");
    await load();
    if (result.errors.length) {
      throw new Error(`数据同步未完成：${result.errors.join("；")}`);
    }
    status.className = "sync-status success";
    status.textContent = `同步完成：${result.marketable_count} 件可出售，更新 ${result.listings_updated} 条挂单状态`;
    toast(status.textContent);
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
$("#sync-new-listings").addEventListener("click", () => busy($("#sync-new-listings"), async () => {
  const result = await post("/api/sync/listings");
  toast(`新上架任务状态已同步，更新 ${result.listings_updated} 条`);
  await load();
}));
$("#select-all-active").addEventListener("change", (event) => {
  document.querySelectorAll(".active-select:not(:disabled)").forEach((checkbox) => {
    checkbox.checked = event.target.checked;
  });
});
$("#select-all-plans").addEventListener("change", (event) => {
  document.querySelectorAll(".plan-select:not(:disabled)").forEach((checkbox) => {
    checkbox.checked = event.target.checked;
  });
});
$("#new-listings-body").addEventListener("change", () => {
  const checkboxes = Array.from(
    document.querySelectorAll(".plan-select:not(:disabled)")
  );
  const checked = checkboxes.filter((checkbox) => checkbox.checked).length;
  $("#select-all-plans").checked = checkboxes.length > 0 && checked === checkboxes.length;
  $("#select-all-plans").indeterminate = checked > 0 && checked < checkboxes.length;
});
$("#cancel-plans").addEventListener("click", () => busy($("#cancel-plans"), async () => {
  const ids = Array.from(document.querySelectorAll(".plan-select:checked"))
    .map((checkbox) => Number(checkbox.dataset.listingId));
  if (!ids.length) throw new Error("请先选择需要取消的新上架任务");
  await post("/api/listings/cancel", { listing_ids: ids });
  toast(`已取消 ${ids.length} 条新上架任务`);
  await load();
}));
$("#price-review-body").addEventListener("click", async (event) => {
  const button = event.target.closest(".review-choice");
  if (!button) return;
  await busy(button, async () => {
    const choice = button.dataset.choice;
    let customBuyerPriceMinor = null;
    if (choice === "custom") {
      const value = window.prompt("输入自定义买家支付价格");
      if (!value) return;
      customBuyerPriceMinor = Math.round(Number(value) * 100);
      if (!Number.isFinite(customBuyerPriceMinor) || customBuyerPriceMinor < 3) {
        throw new Error("自定义价格无效");
      }
    }
    await post(`/api/listings/${button.dataset.listingId}/resolve-price`, {
      choice,
      custom_buyer_price_minor: customBuyerPriceMinor,
    });
    toast(choice === "skip" ? "任务已暂不处理" : "价格已确认并转入新上架任务");
    await load();
  });
});
$("#active-listings-body").addEventListener("change", () => {
  const checkboxes = Array.from(
    document.querySelectorAll(".active-select:not(:disabled)")
  );
  const checked = checkboxes.filter((checkbox) => checkbox.checked).length;
  $("#select-all-active").checked = checkboxes.length > 0 && checked === checkboxes.length;
  $("#select-all-active").indeterminate = checked > 0 && checked < checkboxes.length;
});
$("#active-listings-body").addEventListener("click", async (event) => {
  const button = event.target.closest(".active-blacklist-toggle");
  if (!button) return;
  await busy(button, async () => {
    const item = groupedActiveListings[Number(button.dataset.groupIndex)];
    if (item.blacklisted) {
      const query = new URLSearchParams({
        appid: String(item.appid),
        market_hash_name: item.market_hash_name,
      });
      await remove(`/api/blacklist?${query}`);
      toast(`${item.market_hash_name} 已移出黑名单`);
    } else {
      await post("/api/blacklist", {
        appid: item.appid,
        market_hash_name: item.market_hash_name,
      });
      toast(`${item.market_hash_name} 已加入黑名单，后续不会自动调价`);
    }
    await load();
  });
});
$("#reprice-active").addEventListener("click", () => busy($("#reprice-active"), async () => {
  const ids = Array.from(document.querySelectorAll(".active-select:checked"))
    .flatMap((checkbox) =>
      groupedActiveListings[Number(checkbox.dataset.groupIndex)].listingIds
    );
  if (!ids.length) throw new Error("请先选择至少一个当前在售挂单");
  const results = await post("/api/listings/reprice", {
    listing_ids: ids,
    strategy_profile_id: Number($("#reprice-strategy").value),
    currency: $("#currency").value,
    confirmation_text: "我确认执行真实市场操作",
  });
  const failures = results.filter((item) => item.error_message);
  toast(failures.length
    ? `调价完成，但 ${failures.length} 项失败，请查看结果栏`
    : `已提交 ${results.length} 项调价，等待手机确认`);
  await load();
}));
$("#create-plans").addEventListener("click", () => busy($("#create-plans"), async () => {
  const selectedAssetids = Array.from(document.querySelectorAll(".inventory-select:checked"))
    .flatMap((checkbox) => groupedInventory[Number(checkbox.dataset.groupIndex)].assetids);
  if (!selectedAssetids.length) throw new Error("请先选择至少一项库存");
  const status = $("#sync-status");
  status.className = "sync-status";
  status.textContent = "正在自动同步所选饰品的30天价格并生成计划…";
  const plans = await post("/api/listings/plan", {
    assetids: selectedAssetids,
    strategy_profile_id: Number($("#plan-strategy").value),
    strategy: "robust_median",
    currency: $("#currency").value,
    minimum_buyer_price_minor: Math.round(Number($("#minimum-price").value) * 100),
    maximum_buyer_price_minor: Math.round(Number($("#maximum-price").value) * 100),
    maximum_items: Number($("#maximum-items").value),
  });
  status.className = "sync-status success";
  status.textContent = `价格同步与计划生成完成：${plans.length}条`;
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
    ...currentAppSettings,
    currency: $("#currency").value,
    inventory_pressure_enabled: $("#inventory-pressure-enabled").checked,
    inventory_pressure_threshold: Number($("#inventory-pressure-threshold").value),
  });
  currentAppSettings = saved;
  toast("设置已保存");
}));
$("#strategy-profile-select").addEventListener("change", (event) => {
  const profile = strategyProfiles.find((item) => item.id === Number(event.target.value));
  if (profile) renderStrategyEditor(profile);
});
$("#add-stage").addEventListener("click", () => {
  editingStrategy.stages.push(defaultStage());
  renderStrategyEditor(editingStrategy);
});
$("#strategy-stages").addEventListener("click", (event) => {
  const card = event.target.closest(".stage-card");
  if (!card) return;
  editingStrategy = { ...editingStrategy, ...collectStrategyEditor() };
  const index = Number(card.dataset.stageIndex);
  if (event.target.closest(".stage-delete")) {
    editingStrategy.stages.splice(index, 1);
  } else if (event.target.closest(".stage-up") && index > 0) {
    [editingStrategy.stages[index - 1], editingStrategy.stages[index]] =
      [editingStrategy.stages[index], editingStrategy.stages[index - 1]];
  } else if (event.target.closest(".stage-down") && index < editingStrategy.stages.length - 1) {
    [editingStrategy.stages[index + 1], editingStrategy.stages[index]] =
      [editingStrategy.stages[index], editingStrategy.stages[index + 1]];
  } else {
    return;
  }
  renderStrategyEditor(editingStrategy);
});
$("#new-strategy").addEventListener("click", () => {
  renderStrategyEditor({ id: null, name: "新策略", is_default: false, stages: [defaultStage()] });
});
$("#duplicate-strategy").addEventListener("click", () => {
  const copy = collectStrategyEditor();
  renderStrategyEditor({ id: null, ...copy, name: `${copy.name} 副本`, is_default: false });
});
$("#save-strategy").addEventListener("click", () => busy($("#save-strategy"), async () => {
  const payload = collectStrategyEditor();
  const saved = editingStrategy.id
    ? await put(`/api/strategy-profiles/${editingStrategy.id}`, payload)
    : await post("/api/strategy-profiles", payload);
  editingStrategy = saved;
  toast("策略已保存");
  await load();
}));
$("#delete-strategy").addEventListener("click", () => busy($("#delete-strategy"), async () => {
  if (!editingStrategy.id) throw new Error("尚未保存的新策略无需删除");
  await remove(`/api/strategy-profiles/${editingStrategy.id}`);
  editingStrategy = null;
  toast("策略已删除");
  await load();
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
  const ids = currentListings
    .filter((item) => ["planned", "failed"].includes(item.state))
    .map((item) => item.id);
  if (!ids.length) throw new Error("没有待提交计划");
  const results = await post("/api/listings/execute", {
    listing_ids: ids,
    confirmation_text: "我确认执行真实市场操作",
  });
  const failures = results.filter((item) => item.state === "failed");
  toast(failures.length
    ? `${failures.length} 项提交失败，请查看“结果/失败原因”`
    : `已提交 ${results.length} 项，等待 Steam 手机确认`);
  await load();
}));
$("#execute-selected-plans").addEventListener("click", () => busy($("#execute-selected-plans"), async () => {
  const ids = Array.from(document.querySelectorAll(".plan-select:checked"))
    .map((checkbox) => Number(checkbox.dataset.listingId));
  if (!ids.length) throw new Error("请先选择需要处理的新上架任务");
  const results = await post("/api/listings/execute", {
    listing_ids: ids,
    confirmation_text: "我确认执行真实市场操作",
  });
  const failures = results.filter((item) => item.state === "failed");
  toast(failures.length
    ? `${failures.length} 项提交失败，请查看结果`
    : `已处理 ${results.length} 项，等待 Steam 手机确认`);
  await load();
}));
load();
