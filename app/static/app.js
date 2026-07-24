const $ = (selector) => document.querySelector(selector);

async function getJSON(path) {
  const response = await fetch(path);
  if (!response.ok) throw new Error(`请求失败：${response.status}`);
  return response.json();
}

async function load() {
  try {
    const [health, session, dashboard, strategies] = await Promise.all([
      getJSON("/api/health"),
      getJSON("/api/session"),
      getJSON(`/api/dashboard?currency=${$("#currency").value}`),
      getJSON("/api/strategies"),
    ]);
    $("#health").textContent = health.status === "ok" ? "本地服务正常" : "服务异常";
    $("#session-state").textContent = session.state;
    $("#session-message").textContent = session.message;
    $("#sellable").textContent = dashboard.sellable_items;
    $("#pending").textContent = dashboard.pending_confirmation;
    $("#active").textContent = dashboard.active;
    $("#strategies").innerHTML = strategies
      .map((item) => `<div class="strategy"><strong>${item.name}</strong><small>${item.id}</small></div>`)
      .join("");
  } catch (error) {
    $("#health").textContent = error.message;
  }
}

$("#currency").addEventListener("change", load);
load();

