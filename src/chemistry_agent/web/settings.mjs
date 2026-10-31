const $ = (id) => document.getElementById(id);
let polling;
let busy = false;

async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { "Content-Type": "application/json" } });
  const data = await response.json().catch(() => null);
  if (response.status === 401) { location.assign("/"); throw new Error("请先登录研究空间。"); }
  if (!response.ok) throw new Error(data?.error?.message || "设置未保存，请检查输入格式。也请确认远程服务使用 HTTPS。");
  return data;
}

function notice(text, error = false) {
  $("notice").textContent = text;
  $("notice").classList.toggle("error", error);
}

function togglePanels() {
  const chatgpt = $("provider").value === "chatgpt";
  $("chatgpt-panel").hidden = !chatgpt;
  $("api-panel").hidden = chatgpt;
  $("model-optional").hidden = !chatgpt;
  $("model").required = !chatgpt;
  $("base-url").required = !chatgpt;
  $("base-url").disabled = chatgpt;
  $("api-key").disabled = chatgpt;
  $("model").placeholder = chatgpt ? "留空使用 Codex 默认模型" : "填写服务商提供的模型 ID";
  $("model-hint").textContent = chatgpt ? "可填写当前账号支持的 Codex 模型 ID。" : "模型需要支持 Chat Completions 及工具调用，才能进行自动建模和计算。";
  $("thinking-label").hidden = $("provider").value !== "deepseek";
}

function displaySettings(data) {
  $("provider").value = data.provider;
  $("base-url").value = data.base_url;
  $("model").value = data.model;
  $("thinking").checked = data.thinking;
  $("api-key").value = "";
  $("key-status").textContent = data.has_api_key ? "已保存密钥。留空可保留；更换服务地址或类型时须重新填写。" : "尚未保存 API Key。";
  togglePanels();
}

async function save() {
  if (!$("model-form").reportValidity()) throw new Error("请完整填写模型设置。");
  const data = await api("/api/model/settings", { method: "PUT", body: JSON.stringify({
    provider: $("provider").value, model: $("model").value.trim(),
    base_url: $("provider").value === "chatgpt" ? "https://api.deepseek.com" : $("base-url").value.trim(),
    api_key: $("api-key").value || null, thinking: $("thinking").checked,
  }) });
  displaySettings(data);
}

async function action(operation) {
  if (busy) return;
  busy = true;
  document.querySelectorAll("button").forEach((button) => { button.disabled = true; });
  try { await operation(); }
  catch (error) { notice(error.message || "无法连接服务，请稍后重试。", true); }
  finally {
    busy = false;
    document.querySelectorAll("button").forEach((button) => { button.disabled = false; });
  }
}

async function refreshLogin() {
  const data = await api("/api/model/chatgpt/status");
  const labels = { starting: "正在启动授权…", waiting: "请在官方页面完成授权。", complete: "授权完成。", failed: "登录失败，请重试。设备码模式需在 ChatGPT 安全设置中启用。", expired: "授权已超时，请重新登录。", cancelled: "已取消登录。", idle: "尚未登录 ChatGPT。" };
  $("chatgpt-status").textContent = !data.available ? "服务电脑未找到 Codex CLI。安装后重启服务即可使用账号登录。" : data.authenticated ? "已登录 ChatGPT，可以保存设置并测试连接。" : labels[data.login.state] || "尚未登录。";
  $("chatgpt-logout").hidden = !data.authenticated;
  const url = data.login.url;
  const valid = url && new URL(url).origin === "https://auth.openai.com";
  $("authorization").hidden = !valid;
  if (valid) $("authorization-link").href = url;
  else $("authorization-link").removeAttribute("href");
  $("device-code-row").hidden = !data.login.code;
  $("device-code").textContent = data.login.code || "";
  clearTimeout(polling);
  if (["starting", "waiting"].includes(data.login.state)) {
    polling = setTimeout(() => refreshLogin().catch((error) => notice(error.message, true)), 2000);
  }
}

$("provider").addEventListener("change", () => {
  $("api-key").value = "";
  $("model").value = $("provider").value === "deepseek" ? "deepseek-flash" : "";
  if ($("provider").value === "deepseek") $("base-url").value = "https://api.deepseek.com";
  else if ($("provider").value === "openai-compatible") $("base-url").value = "";
  $("key-status").textContent = "更换服务时，请填写对应 API Key。";
  togglePanels();
});
$("model-form").addEventListener("submit", (event) => {
  event.preventDefault();
  action(async () => { await save(); notice("已保存。返回研究空间即可使用新的模型设置。"); });
});
$("test-model").addEventListener("click", () => action(async () => {
  await save();
  notice("设置已保存，正在测试 AI 响应…");
  const result = await api("/api/model/test", { method: "POST" });
  notice(`连接成功：${result.answer}`);
}));
for (const method of ["browser", "device"]) {
  $(`${method}-login`).addEventListener("click", () => action(async () => {
    await save();
    await api("/api/model/chatgpt/login", { method: "POST", body: JSON.stringify({ method }) });
    notice("已启动 ChatGPT 授权，请完成官方登录页面上的操作。");
    await refreshLogin();
  }));
}
$("cancel-login").addEventListener("click", () => action(async () => {
  await api("/api/model/chatgpt/cancel", { method: "POST" });
  await refreshLogin();
}));
$("chatgpt-logout").addEventListener("click", () => action(async () => {
  await api("/api/model/chatgpt/logout", { method: "POST" });
  await refreshLogin();
  notice("已退出本研究空间的 ChatGPT 登录。");
}));
$("clear-key").addEventListener("click", () => action(async () => {
  await api("/api/model/settings/key", { method: "DELETE" });
  displaySettings(await api("/api/model/settings"));
  notice("已删除保存的 API Key，并切换为 ChatGPT 登录方式。");
}));

action(async () => {
  displaySettings(await api("/api/model/settings"));
  await refreshLogin();
});
window.addEventListener("pagehide", () => clearTimeout(polling));

const theme = globalThis.chemistryTheme;
function displayAppearance() {
  const value = theme.read();
  $("theme-mode").value = value.mode;
  $("accent-color").value = value.accent;
  document.querySelectorAll("[data-accent]").forEach((button) => {
    button.setAttribute("aria-pressed", String(button.dataset.accent === value.accent));
  });
}
function saveAppearance() {
  const saved = theme.save({ mode: $("theme-mode").value, accent: $("accent-color").value });
  $("appearance-notice").textContent = saved ? "已保存到当前浏览器。" : "已应用；浏览器禁止存储，刷新后可能恢复默认。";
  if (saved) displayAppearance();
}
$("theme-mode").addEventListener("change", saveAppearance);
$("accent-color").addEventListener("input", saveAppearance);
for (const button of document.querySelectorAll("[data-accent]")) {
  button.addEventListener("click", () => { $("accent-color").value = button.dataset.accent; saveAppearance(); });
}
$("reset-appearance").addEventListener("click", () => {
  $("theme-mode").value = "light"; $("accent-color").value = "#5865b5"; saveAppearance();
});
displayAppearance();
window.addEventListener("storage", (event) => { if (event.key === "chemistry.appearance") displayAppearance(); });
api("/auth/status").then((data) => {
  const name = data.user?.username || "本地账户";
  $("settings-username").textContent = name;
  $("settings-avatar").textContent = name.slice(0, 1).toUpperCase();
  $("settings-account-note").textContent = data.enabled ? "个人工作空间 · 已登录" : "本地单用户模式";
  $("settings-logout").hidden = !data.enabled;
}).catch((error) => notice(error.message, true));
$("settings-logout").addEventListener("click", () => action(async () => {
  await api("/auth/logout", { method: "POST" }); location.assign("/");
}));
