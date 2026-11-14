import { createAgentSettings } from "./agent-settings.mjs";
import { t, getLanguage, setLanguage, translateUI } from "./i18n.mjs";
import { fillModelOptions } from "./models.mjs";
import { createComputeSettings } from "./compute-settings.mjs";
const $ = (id) => document.getElementById(id);
let polling;
let busy = false;
let savedSettings = null;
let models = [];
let lastAuth = null;
let chatgptAuthenticated = false;
let accounts = [];

// Only the same-origin workspace may host the settings controls and their messages.
let embedded = false;
if (parent !== window && new URLSearchParams(location.search).get("embed") === "1") {
  try { embedded = parent.location.origin === location.origin; } catch { /* Foreign host. */ }
}
document.documentElement.classList.toggle("embedded-settings", embedded);
const compactLayout = (embedded ? parent : window).matchMedia("(max-width:767px)");
function syncNavigationLayout() {
  document.documentElement.classList.toggle("compact-settings", compactLayout.matches);
  document.querySelector(".settings-sidebar nav").setAttribute("aria-orientation", compactLayout.matches ? "horizontal" : "vertical");
}
syncNavigationLayout();
compactLayout.addEventListener("change", syncNavigationLayout);
function closeSettings() {
  if (embedded) parent.postMessage({type:"chem:close-settings"}, location.origin);
  else location.assign("/");
}
document.querySelector(".close-settings").addEventListener("click", (event) => {
  if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  event.preventDefault(); closeSettings();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !document.querySelector("dialog[open]")) {
    event.preventDefault(); closeSettings();
  }
});

async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { "Content-Type": "application/json" } });
  const data = await response.json().catch(() => null);
  if (response.status === 401) { (embedded ? parent : window).location.assign("/"); throw new Error("请先登录研究空间。"); }
  if (!response.ok) throw new Error(data?.error?.message || "设置未保存，请检查输入格式。也请确认远程服务使用 HTTPS。");
  return data;
}

function notice(text, error = false) {
  $("notice").textContent = text;
  $("notice").classList.toggle("error", error);
}

function showModels() {
  fillModelOptions($("model-choice"), models, $("model").value.trim(), {
    defaultLabel: $("provider").value === "chatgpt" ? "自动选择" : "请选择模型",
    manual: $("provider").value !== "chatgpt",
  });
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
  $("model").placeholder = chatgpt ? "留空自动选择可用模型" : "填写服务商提供的模型 ID";
  $("thinking-label").hidden = $("provider").value !== "deepseek";
  showModels();
}
function displaySettings(data) {
  savedSettings = data;
  document.dispatchEvent(new CustomEvent("chem:model-settings", {detail:{provider:data.provider,model:data.model,configured:data.provider === "chatgpt" ? undefined : Boolean(data.model && (data.has_api_key || data.provider === "openai-compatible"))}}));
  $("provider").value = data.provider;
  $("base-url").value = data.base_url;
  $("model").value = data.model;
  $("thinking").checked = data.thinking;
  $("api-key").value = "";
  $("key-status").textContent = data.has_api_key ? "已保存密钥。" : "尚未保存 API Key。";
  $("model-advanced").open = data.provider !== "chatgpt" && !data.model;
  togglePanels();
}
async function refreshModels(manual = false) {
  if ($("provider").value === "chatgpt" && !chatgptAuthenticated) {
    models = []; showModels();
    $("catalog-status").textContent = manual ? "请先登录 ChatGPT。" : "";
    return;
  }
  if (!savedSettings || $("provider").value !== savedSettings.provider ||
      (savedSettings.provider !== "chatgpt" && $("base-url").value.trim().replace(/\/$/, "") !== savedSettings.base_url)) {
    models = []; showModels();
    $("catalog-status").textContent = "请先保存连接设置。"; return;
  }
  $("catalog-status").textContent = "正在读取模型列表…";
  try {
    const result = await api("/api/model/models");
    models = result.models;
    showModels();
    $("catalog-status").textContent = models.length ? "" : "未发现模型。";
  } catch (error) {
    models = []; showModels();
    $("catalog-status").textContent = error.message;
  }
}
async function save() {
  if (!$("model-form").reportValidity()) throw new Error("请完整填写模型设置。");
  displaySettings(await api("/api/model/settings", { method: "PUT", body: JSON.stringify({
    provider: $("provider").value, model: $("model").value.trim(),
    base_url: $("provider").value === "chatgpt" ? "https://api.deepseek.com" : $("base-url").value.trim(),
    api_key: $("api-key").value || null, thinking: $("thinking").checked,
  }) }));
}
async function action(operation) {
  if (busy) return;
  busy = true;
  $("model-form").querySelectorAll("button,select").forEach((node) => { node.disabled = true; });
  try { await operation(); }
  catch (error) { notice(error.message || "无法连接服务，请稍后重试。", true); }
  finally {
    busy = false;
    $("model-form").querySelectorAll("button,select").forEach((node) => { node.disabled = false; });
    togglePanels();
  }
}
async function refreshLogin() {
  const data = await api("/api/model/chatgpt/status");
  chatgptAuthenticated = Boolean(data.authenticated);
  if (savedSettings?.provider === "chatgpt") document.dispatchEvent(new CustomEvent("chem:model-settings", {detail:{provider:"chatgpt",model:savedSettings.model,configured:chatgptAuthenticated}}));
  const labels = { starting: "正在启动授权…", waiting: "请在官方页面完成授权。", complete: "授权完成。", failed: "登录失败，请重新授权。", expired: "授权已超时，请重新登录。", cancelled: "已取消登录。", idle: "尚未登录 ChatGPT。" };
  const pending = ["starting", "waiting"].includes(data.login.state);
  $("chatgpt-status").textContent = pending ? labels[data.login.state] : data.authenticated
    ? data.authentication_mode === "codex" ? "已通过 Codex 连接。" : "已登录 ChatGPT。"
    : labels[data.login.state] || "尚未登录。";
  if (data.login.state === "failed" && data.login.error) notice(data.login.error, true);
  $("chatgpt-logout").hidden = !data.authenticated;
  $("device-login").disabled = busy || data.legacy_available === false;
  accounts = data.accounts || [];
  $("chatgpt-account").replaceChildren(new Option(t("选择已保存的账号"), ""), ...(data.accounts || []).map((item) => new Option(item.label, item.id)));
  $("chatgpt-account").value = data.active_account || "";
  const url = data.login.url;
  let valid = false;
  try { valid = url && ["https://auth.openai.com", "https://chatgpt.com"].includes(new URL(url).origin); } catch { /* Ignore invalid URLs. */ }
  $("authorization").hidden = !valid;
  if (valid) $("authorization-link").href = url;
  else $("authorization-link").removeAttribute("href");
  $("device-code-row").hidden = !data.login.code;
  $("device-code").textContent = data.login.code || "";
  clearTimeout(polling);
  if (pending) polling = setTimeout(() => refreshLogin().catch((error) => notice(error.message, true)), 2000);
  const identity = `${data.authentication_mode}:${data.active_account}:${data.authenticated}`;
  if (lastAuth !== null && lastAuth !== identity) await refreshModels();
  lastAuth = identity;
}
$("provider").addEventListener("change", () => {
  models = [];
  $("api-key").value = "";
  $("model").value = $("provider").value === "deepseek" ? "deepseek-flash" : "";
  if ($("provider").value === "deepseek") $("base-url").value = "https://api.deepseek.com";
  else if ($("provider").value === "openai-compatible") $("base-url").value = "";
  $("model-advanced").open = $("provider").value !== "chatgpt" && !$("model").value;
  $("catalog-status").textContent = "请先保存连接设置。";
  $("key-status").textContent = "更换服务时，请填写对应 API Key。";
  togglePanels();
});
$("base-url").addEventListener("input", () => { models = []; showModels(); $("catalog-status").textContent = "请先保存连接设置。"; });
$("model-choice").addEventListener("change", () => {
  if ($("model-choice").value === "__manual__") { $("model-advanced").open = true; $("model").focus(); return; }
  $("model").value = $("model-choice").value;
});
$("model").addEventListener("input", showModels);
$("refresh-models").addEventListener("click", () => action(() => refreshModels(true)));
$("model-form").addEventListener("submit", (event) => {
  event.preventDefault();
  action(async () => { await save(); notice("已保存。返回研究空间即可使用新的模型设置。"); await refreshModels(); });
});
$("test-model").addEventListener("click", () => action(async () => {
  await save(); notice("设置已保存，正在测试 AI 响应…");
  const result = await api("/api/model/test", { method: "POST" });
  notice(`连接成功：${result.answer}`);
}));
async function login(method, newAccount = false) {
  const accountId = newAccount ? null : $("chatgpt-account").value || null;
  await save();
  await api("/api/model/chatgpt/login", { method: "POST", body: JSON.stringify({ method, new_account: newAccount, account_id: accountId }) });
  notice("已启动 ChatGPT 授权，请完成官方登录页面上的操作。");
  await refreshLogin();
}
for (const method of ["browser", "device"]) $(`${method}-login`).addEventListener("click", () => action(() => login(method)));
$("add-account").addEventListener("click", () => action(() => login("browser", true)));
$("chatgpt-account").addEventListener("change", () => action(async () => {
  if (!$("chatgpt-account").value) return;
  if (!accounts.find((item) => item.id === $("chatgpt-account").value)?.authenticated) {
    await login("browser"); return;
  }
  await api("/api/model/chatgpt/account", { method: "POST", body: JSON.stringify({ account_id: $("chatgpt-account").value }) });
  await refreshLogin();
}));
$("cancel-login").addEventListener("click", () => action(async () => { await api("/api/model/chatgpt/cancel", { method: "POST" }); await refreshLogin(); }));
$("chatgpt-logout").addEventListener("click", () => action(async () => {
  const result = await api("/api/model/chatgpt/logout", { method: "POST" });
  await refreshLogin();
  notice(result?.remote_revoked === false ? "已退出本地登录，远端撤销未确认；可在 ChatGPT 用量设置中断开应用。" : "已退出本研究空间的 ChatGPT 登录。");
}));
$("clear-key").addEventListener("click", () => action(async () => {
  await api("/api/model/settings/key", { method: "DELETE" });
  displaySettings(await api("/api/model/settings")); await refreshModels();
  notice("已删除保存的 API Key，并切换为 ChatGPT 登录方式。");
}));
let modelLoaded = false;
window.addEventListener("pagehide", () => clearTimeout(polling));

const theme = globalThis.chemistryTheme;
function displayAppearance() {
  const value = theme.read();
  selectedMode = value.mode;
  document.querySelectorAll("[data-mode]").forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.mode === value.mode)));
  $("chat-font-size").value = String(value.fontSize);
}
let selectedMode = theme.read().mode;
function saveAppearance() {
  const saved = theme.save({ mode: selectedMode, fontSize: Number($("chat-font-size").value) });
  $("appearance-notice").textContent = saved ? "" : "浏览器未能保存外观设置。";
  if (saved) displayAppearance();
}
document.querySelectorAll("[data-mode]").forEach((button) => button.addEventListener("click", () => { selectedMode = button.dataset.mode; saveAppearance(); }));
$("chat-font-size").addEventListener("change", saveAppearance);
displayAppearance();
window.addEventListener("storage", (event) => { if (event.key === "chemistry.appearance") displayAppearance(); });
api("/auth/status").then((data) => {
  const name = data.user?.username || "本地账户";
  if (data.user) $("settings-username").setAttribute("data-no-i18n", "");
  $("settings-username").textContent = name;
  $("settings-avatar").textContent = data.user ? name.slice(0, 1).toUpperCase() : "C";
  $("settings-account-note").textContent = data.enabled ? "个人工作空间 · 已登录" : "本地单用户模式";
  $("settings-logout").hidden = !data.enabled;
}).catch((error) => notice(error.message, true));
$("settings-logout").addEventListener("click", () => action(async () => {
  await api("/auth/logout", { method: "POST" }); (embedded ? parent : window).location.assign("/");
}));

const computeSettings = createComputeSettings(selectTab);
const agentSettings = createAgentSettings(api);
function showSettingsPanel() {
  const aliases = { account: "general", "model-form": "agent", model: "agent" };
  const hash = embedded ? new URLSearchParams(location.search).get("tab") : location.hash.slice(1);
  const selected = aliases[hash] || (tabs.some(tab => tab.dataset.tab === hash) ? hash : "general");
  document.querySelectorAll("[data-panel]").forEach((panel) => { panel.hidden = panel.dataset.panel !== selected; });
  document.querySelectorAll("[data-tab]").forEach((tab) => {
    tab.setAttribute("aria-selected", String(tab.dataset.tab === selected));
    tab.tabIndex = tab.dataset.tab === selected ? 0 : -1;
  });
  $("notice").hidden = selected !== "agent";
  document.querySelector("main").scrollTop = 0;
  if (compactLayout.matches) document.querySelector('[data-tab][aria-selected="true"]').scrollIntoView({block:"nearest", inline:"nearest"});
  if (embedded) parent.postMessage({type:"chem:settings-tab", tab:selected}, location.origin);
  computeSettings.activate(selected);
  if (selected === "agent") void agentSettings.load();
  if (selected === "agent" && !modelLoaded) {
    modelLoaded = true;
    action(async () => {
      try { displaySettings(await api("/api/model/settings")); await refreshLogin(); await refreshModels(); }
      catch (error) { modelLoaded = false; throw error; }
    });
  }
}
const tabs = [...document.querySelectorAll("[data-tab]")];
function selectTab(key) {
  // Nested documents must not push history entries ahead of the host dialog.
  if (!embedded && location.hash !== "#" + key) {
    location.hash = key;
    return;
  }
  if (embedded) {
    const url = new URL(location.href);
    url.searchParams.set("tab", key);
    history.replaceState(null, "", url);
  }
  showSettingsPanel();
}
if (embedded) document.addEventListener("click", (event) => {
  const link = event.target.closest?.('a[href^="#"]');
  if (link && tabs.some(tab => tab.dataset.tab === link.hash.slice(1))) {
    event.preventDefault(); selectTab(link.hash.slice(1));
  }
});
tabs.forEach((tab, index) => {
  tab.addEventListener("click", () => selectTab(tab.dataset.tab));
  tab.addEventListener("keydown", (event) => {
    const offset = {ArrowDown:1, ArrowRight:1, ArrowUp:-1, ArrowLeft:-1}[event.key];
    if (!offset && !["Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : (index + offset + tabs.length) % tabs.length;
    tabs[next].click(); tabs[next].focus();
  });
});
window.addEventListener("hashchange", showSettingsPanel);
showSettingsPanel();
$("language").value = getLanguage();
$("language").addEventListener("change", () => setLanguage($("language").value));
window.addEventListener("languagechange", () => { $("language").value = getLanguage(); translateUI(); });
