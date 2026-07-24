const $ = (selector) => document.querySelector(selector);

async function getJSON(path) {
  const response = await fetch(path);
  if (!response.ok) throw new Error(`请求失败：${response.status}`);
  return response.json();
}

async function post(path) {
  const response = await fetch(path, { method: "POST" });
  if (!response.ok) throw new Error(`请求失败：${response.status}`);
  return response.json();
}

function renderSession(session) {
  $("#session-state").textContent = session.state;
  $("#session-message").textContent = session.message;
  $("#login").hidden = session.state === "logged_in";
  $("#logout").hidden = session.state !== "logged_in";
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
    renderSession(session);
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

$("#login").addEventListener("click", async () => {
  $("#login").disabled = true;
  $("#session-state").textContent = "logging_in";
  $("#session-message").textContent = "请在弹出的 Steam 官方窗口完成登录，此页面会自动更新";
  try {
    const result = await post("/api/session/login");
    renderSession(result.status);
  } catch (error) {
    $("#session-message").textContent = error.message;
  } finally {
    $("#login").disabled = false;
  }
});

$("#logout").addEventListener("click", async () => {
  $("#logout").disabled = true;
  try {
    const result = await post("/api/session/logout");
    renderSession(result.status);
  } catch (error) {
    $("#session-message").textContent = error.message;
  } finally {
    $("#logout").disabled = false;
  }
});

$("#currency").addEventListener("change", load);
load();
