import { initInspectorTabs } from "./inspector-tabs.mjs";
import { createStructureBridge, structureButton, inlineStructureName, isStructureName, getFileBlob } from "./structure-links.mjs";
import { createCalculations } from "./calculations.mjs?v=20261101-inspector";
import { createNotifications } from "./notifications.mjs";
import { createChatModelPicker } from "./chat-models.mjs";
import { t } from "./i18n.mjs";
import { createFileUploads, renderFiles, contextFiles } from "./files.mjs";
import { createProjectNavigation } from "./projects.mjs";
import { TOOL_LABELS, buildTurns, contextBadge, createMarkdownRenderer, escapeHtml, metricValue } from "./core.mjs";

const $ = (id) => document.getElementById(id);
const icon = (name, className = "") => `<svg class="icon ${className}" aria-hidden="true"><use href="#i-${name}"/></svg>`;
const markdown = globalThis.markdownit ? createMarkdownRenderer(globalThis.markdownit) : {
  render: (text) => `<p>${escapeHtml(text).replace(/\n/g, "<br>")}</p>`,
};
const state = {
  sessions: [], sessionId: null, turns: [], runs: [], pending: null, health: null,
  busy: false, loading: false, hasMore: false, navigation: 0, results: new Map(),
  auth: { enabled: false, user: null }, accountVersion: 0,
};
let toastTimer;
let chatModels;
const notifications = createNotifications({ api });
const selectInspectorTab = initInspectorTabs(() => {
  setDrawer("sidebar", false);
  setInspectorOpen(true, { focus:false });
});
const viewer = createStructureBridge({selectTab:selectInspectorTab, toast});
const calculations = createCalculations({
  viewer, chatFiles: () => [...state.turns.flatMap(turn => turn.files?.length ? turn.files : contextFiles(turn.context)), ...uploads.files],
  api, getSession: () => state.sessionId,
  isBusy: () => state.busy || state.loading || chatModels?.saving || uploads.uploading,
  ensureSession: async () => {
    if (!state.sessionId) {
      const selectedModel = chatModels.value;
      const session = await api("/sessions", {method:"POST", body:JSON.stringify({title:"结构与计算"})});
      state.sessionId = session.id; rememberSession(session.id);
      $("conversation-title").textContent = session.title;
      await chatModels.saveNewSession(session.id, selectedModel);
      await projectNavigation.assign(session.id);
      await refreshSessions();
    }
    return state.sessionId;
  },
  toast, onConfirmed: async (id) => { await selectSession(id); await refreshSessions(); },
});

class ApiError extends Error {
  constructor(message, data, status) { super(message); this.data = data; this.status = status; }
}

async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(path, { ...options, headers: { "Content-Type": "application/json", ...options.headers } });
  } catch {
    throw new ApiError("暂时无法连接本地服务，请检查服务是否正在运行。", null, 0);
  }
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    const message = data?.error?.message || data?.result?.message ||
      (response.status === 422 ? "输入格式有误，请检查后再试。" : "请求未能完成，请稍后重试。");
    if (response.status === 401 && data?.error?.code === "authentication_required") {
      showLogin("登录已过期，请重新登录。");
    }
    throw new ApiError(message, data, response.status);
  }
  return data;
}

function toast(message) {
  clearTimeout(toastTimer);
  $("toast").textContent = message;
  $("toast").hidden = false;
  toastTimer = setTimeout(() => { $("toast").hidden = true; }, 4200);
}

function rememberSession(id) {
  const storageKey = state.auth.user ? `chemistry.session.${state.auth.user.id}` : "chemistry.session";
  try {
    if (id) localStorage.setItem(storageKey, id);
    else localStorage.removeItem(storageKey);
  } catch { /* Storage can be unavailable in a private browser. */ }
  const url = new URL(location.href);
  if (id) url.searchParams.set("session", id);
  else url.searchParams.delete("session");
  history.replaceState(null, "", url);
}

function syncDrawerAccessibility() {
  const loginRequired = !$("login-screen").hidden;
  const sidebarHidden = loginRequired || (innerWidth <= 760 && !document.body.classList.contains("sidebar-open") && !document.body.classList.contains("sidebar-collapsed"));
  const inspectorHidden = loginRequired || !document.body.classList.contains("inspector-open");
  const mainHidden = loginRequired || (innerWidth <= 760 && !inspectorHidden);
  document.querySelector(".main-panel").inert = mainHidden;
  document.querySelector(".main-panel").setAttribute("aria-hidden", String(mainHidden));
  $("sidebar").inert = sidebarHidden;
  $("inspector").inert = inspectorHidden;
  $("sidebar").setAttribute("aria-hidden", String(sidebarHidden));
  $("inspector").setAttribute("aria-hidden", String(inspectorHidden));
}

function setInspectorOpen(open, { focus = true, persist = true } = {}) {
  const wasFocused = $("inspector").contains(document.activeElement);
  document.body.classList.toggle("inspector-open", open);
  $("tools-toggle").setAttribute("aria-expanded", String(open));
  $("tools-toggle").setAttribute("aria-label", open ? "关闭研究面板" : "打开研究工具");
  $("tools-toggle").title = open ? "关闭研究面板" : "打开研究工具";
  if (persist) {
    try { localStorage.setItem("chemistry.inspectorOpen", String(open)); } catch { /* Optional. */ }
  }
  syncDrawerAccessibility();
  if (focus && open) $("inspector-close").focus();
  else if (focus && wasFocused) $("tools-toggle").focus();
}

function setDrawer(name, open) {
  if (name === "inspector") { setInspectorOpen(open); return; }
  const panel = $(name === "sidebar" ? "sidebar" : "inspector");
  const trigger = $(name === "sidebar" ? "menu-toggle" : "tools-toggle");
  const wasFocused = panel.contains(document.activeElement);
  document.body.classList.toggle(`${name}-open`, open);
  const anyOpen = document.body.classList.contains("sidebar-open");
  $("screen-overlay").hidden = !anyOpen;
  $("menu-toggle").setAttribute("aria-expanded", String(document.body.classList.contains("sidebar-open")));
  $("tools-toggle").setAttribute("aria-expanded", String(document.body.classList.contains("inspector-open")));
  syncDrawerAccessibility();
  if (open) panel.querySelector("button, input, summary")?.focus();
  else if (wasFocused && trigger.getClientRects().length) trigger.focus();
}

function closeDrawers() {
  setDrawer("sidebar", false);
  if (innerWidth <= 760) setInspectorOpen(false, { focus:false });
}

function updateControls() {
  const locked = state.busy || state.loading || chatModels?.saving || calculations.busy();
  $("compact-context").disabled = locked || !state.sessionId || !state.health?.model_configured;
  $("chat-model").disabled = Boolean(locked);
  $("chat-model-apply").disabled = Boolean(locked);
  $("chat-model-id").disabled = Boolean(locked);
  $("message-input").disabled = locked;
  $("send-button").disabled = locked || uploads.uploading || !state.health?.model_configured;
  $("composer-add").disabled = locked || uploads.uploading;
  $("sidebar-add").disabled = locked || uploads.uploading;
  $("send-button").setAttribute("aria-label", state.busy ? "正在生成回答" : "发送消息");
  $("new-chat").disabled = locked;
  $("new-project").disabled = locked;
  document.querySelectorAll("[data-new-in]").forEach((button) => { button.disabled = locked; });
  document.querySelectorAll(".session-button,.suggestion-card").forEach((button) => { button.disabled = locked; });
  $("chat-form").setAttribute("aria-busy", String(state.busy));
}

async function refreshHealth() {
  try {
    const previousProvider = state.health?.provider;
    state.health = await api("/health");
    calculations.enabled(Boolean(state.health.automation_enabled));
    notifications.enabled(Boolean(state.health.automation_enabled));
    $("automation-link").hidden = !state.health.automation_enabled;
    if (chatModels && previousProvider && previousProvider !== state.health.provider) {
      chatModels.newConversation();
      if (state.sessionId) await chatModels.loadSession(state.sessionId);
      await chatModels.refresh();
    }
    chatModels?.render();
    const names = { "deepseek-flash": "DeepSeek Flash", "deepseek-v4-pro": "DeepSeek Pro" };
    $("model-label").textContent = state.health.model_configured
      ? `${names[state.health.model] || state.health.model} · ${state.health.provider === "chatgpt" ? "已登录" : "已配置"}` : "模型未配置";
    $("model-status").classList.toggle("unavailable", !state.health.model_configured);
  } catch {
    state.health = null;
    $("model-label").textContent = "服务未连接";
    $("model-status").classList.add("unavailable");
  }
  updateControls();
}

function sessionButton(session) {
  return `<button class="session-button${session.id === state.sessionId ? " active" : ""}" data-session="${escapeHtml(session.id)}" aria-current="${session.id === state.sessionId ? "page" : "false"}" title="${escapeHtml(session.title)}">${icon("chat")}<span>${escapeHtml(session.title)}</span></button>`;
}

const projectNavigation = createProjectNavigation({ api, state, selectSession, newConversation, refreshSessions, toast, sessionButton });
chatModels = createChatModelPicker({ api, state, toast, updateControls });
const uploads = createFileUploads({ state, toast, updateControls, onChange: () => calculations.drawStructures(), onUnauthorized: () => showLogin("登录已过期，请重新登录。") });

function renderSessions() {
  const chats = state.sessions.filter((session) => !projectNavigation.includes(session.id));
  $("session-list").innerHTML = chats.length ? chats.map(sessionButton).join("") : '<p class="sidebar-empty">暂无独立聊天</p>';
  $("load-more").hidden = !state.hasMore;
  projectNavigation.render();
  updateControls();
}

async function refreshSessions(append = false) {
  const accountVersion = state.accountVersion;
  const offset = append ? state.sessions.length : 0;
  const [sessions, projects] = await Promise.all([api(`/sessions?limit=50&offset=${offset}`), api("/api/projects")]);
  if (accountVersion !== state.accountVersion) return;
  projectNavigation.accept(projects);
  state.sessions = append ? [...state.sessions, ...sessions] : sessions;
  state.hasMore = sessions.length === 50;
  renderSessions();
}

function renderUser(text, context, files = [], structures = []) {
  const attachedFiles = files.length ? files : contextFiles(context);
  return `<div class="user-message"><div class="user-bubble"><span data-no-i18n>${escapeHtml(text)}</span>${renderFiles(attachedFiles)}${structures.map(s => structureButton("artifact",s.id,s.name+".extxyz","查看 "+s.name)).join("")}${context && !attachedFiles.length ?
    `<details class="attached-reference"><summary>${icon("document")} ${t("附件与上下文")}</summary><pre>${escapeHtml(context)}</pre></details>` : ""}</div></div>`;
}

function artifactLinks(calls) {
  const links = new Map();
  for (const call of calls) {
    if (!call.success) continue;
    const r = call.result || {};
    const isStructure = ["build_structure","import_structure","calculation_import_result"].includes(call.name);
    const id = isStructure ? r.id : r.surface_structure_id || r.structure_id;
    if (id) links.set(id, structureButton("artifact",id,(r.name || "structure")+".extxyz","查看"+(r.name || "输入结构")));
    if (r.finished_ok && r.output_structure && r.id) links.set(r.id,structureButton("job",r.id,"optimized.extxyz","查看优化结构"));
    if (call.name.startsWith("calculation_") && !isStructure) links.set("task",'<button class="view-result" data-task-panel>查看计算</button>');
  }
  return [...links.values()].join(" ");
}

function renderToolTrace(calls, turnIndex) {
  if (!calls.length) return "";
  const success = calls.filter((call) => call.success).length;
  return `<details class="tool-trace"><summary>${icon(success === calls.length ? "check" : "flask")}<span>已使用 ${calls.length} 次研究工具${success < calls.length ? ` · ${calls.length - success} 次需调整输入` : ""}</span>${icon("chevron", "disclosure")}</summary><div class="tool-trace-content">${calls.map((call, index) => {
    const id = `${turnIndex}-${index}`;
    state.results.set(id, call);
    return `<div class="tool-trace-item"><strong>${escapeHtml(TOOL_LABELS[call.name] || call.name)}</strong><button type="button" class="view-result" data-result="${id}">查看结果</button><pre>${escapeHtml(JSON.stringify({ arguments: call.arguments, result: call.result }, null, 2))}</pre></div>`;
  }).join("")}</div></details>`;
}

function renderContextBadge(context) {
  const badge = contextBadge(context);
  return badge ? `<span class="context-badge" title="${escapeHtml(badge.description)}" aria-label="${escapeHtml(badge.description)}">${escapeHtml(badge.label)}</span>` : "";
}

function renderConversation(scroll = true) {
  state.results.clear();
  const hasConversation = state.turns.length > 0 || state.pending !== null;
  $("welcome").hidden = hasConversation || state.loading;
  $("conversation").hidden = !hasConversation && !state.loading;
  if (state.loading) {
    $("conversation").innerHTML = '<div class="thinking-indicator">正在打开这段研究对话…</div>';
    return;
  }
  $("conversation").innerHTML = state.turns.map((turn, index) =>
    `<article class="turn">${renderUser(turn.calls.some(c => c.name === "calculation_submit_confirmed") ? turn.text.replace(/^确认提交预览 [0-9a-f-]{36}/i,"确认提交") : turn.text, turn.context, turn.files, turn.structures)}<div class="assistant-message"><span class="assistant-avatar">${icon("molecule")}</span><div class="assistant-body"><div class="assistant-label">Chemistry Agent <span>研究助手</span></div>${renderToolTrace(turn.calls, index)}<div class="markdown">${markdown.render(turn.answer)}</div>${artifactLinks(turn.calls)}<div class="message-footer"><button class="icon-button" data-copy="${index}" title="复制回答" aria-label="复制回答">${icon("copy")}</button><span>${turn.run ? `${turn.run.usage.total_tokens.toLocaleString()} tokens · ` : ""}对话已保存</span>${renderContextBadge(turn.run?.context)}</div></div></div></article>`
  ).join("");
  if (state.pending) {
    const pending = state.pending;
    const article = document.createElement("article");
    article.className = "turn";
    article.innerHTML = `${renderUser(pending.message, pending.context, pending.files, pending.structures)}<div class="assistant-message"><span class="assistant-avatar">${icon("molecule")}</span><div class="assistant-body"><div class="assistant-label">Chemistry Agent <span>研究助手</span></div>${pending.error ? `<div class="message-error">${escapeHtml(pending.error)}<br><button class="retry-button" id="retry-message">${icon("refresh")}重试这条消息</button></div>` : '<div class="thinking-indicator"><span></span><span></span><span></span>正在分析问题…</div>'}</div></div>`;
    $("conversation").append(article);
  }
  for (const code of $("conversation").querySelectorAll(".markdown pre > code, .user-bubble > span[data-no-i18n]")) {
    const name = inlineStructureName(code.textContent, code.className);
    if (!name) continue;
    const button = document.createElement("button");
    button.className = "view-result"; button.textContent = "查看结构";
    button.onclick = () => viewer.open(name, () => new Blob([code.textContent], {type:"text/plain"}));
    (code.tagName === "CODE" ? code.parentElement : code).after(button);
  }
  for (const link of $("conversation").querySelectorAll(".markdown a[href]")) {
    if (!URL.canParse(link.href)) continue;
    const url = new URL(link.href);
    if (url.origin !== location.origin || !state.sessionId) continue;
    const name = link.textContent.trim(), path = url.pathname;
    const uuid = /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i;
    const parts = path.split("/");
    const sessionPath = "/api/automation/chat/"+state.sessionId;
    const isFile = parts.length === 4 && path.startsWith("/api/files/") && uuid.test(parts[3]);
    const isArtifact = parts.length === 8 && path.startsWith(sessionPath+"/files/structure/") && uuid.test(parts[7]);
    const isOutput = parts.length === 8 && path.startsWith(sessionPath+"/jobs/") && uuid.test(parts[6]) && parts[7] === "structure.extxyz";
    const filename = isStructureName(name) ? name : state.turns.flatMap(turn => turn.files || contextFiles(turn.context)).find(f => f.id === parts[3])?.name;
    if (!(isFile && isStructureName(filename)) && !isArtifact && !isOutput) continue;
    const button = document.createElement("button");
    button.className = "view-result"; button.textContent = "查看结构";
    button.onclick = () => viewer.open(isFile ? filename : "structure.extxyz", () => getFileBlob(path));
    link.after(button);
  }
  calculations.drawStructures();
  if (scroll) requestAnimationFrame(() => {
    $("chat-scroll").scrollTo({ top: $("chat-scroll").scrollHeight, behavior: "smooth" });
  });
  updateControls();
}

async function selectSession(id, quiet = false) {
  if (state.busy) return;
  if (id !== state.sessionId) { state.sessionId = null; state.turns = []; viewer.reset(); calculations.reset(); }
  uploads.clear();
  calculations.clearSelection();
  const navigation = ++state.navigation;
  state.loading = true;
  state.pending = null;
  renderConversation(false);
  closeDrawers();
  try {
    // Message history is authoritative. All tool details can be reconstructed from it.
    const session = await api(`/sessions/${encodeURIComponent(id)}`);
    const runs = await api(`/sessions/${encodeURIComponent(id)}/runs?limit=100`).catch(() => []);
    if (navigation !== state.navigation) return;
    await chatModels.loadSession(id);
    if (navigation !== state.navigation) return;
    state.sessionId = id;
    state.runs = runs;
    state.turns = buildTurns(session.messages, runs);
    $("conversation-title").textContent = session.title;
    rememberSession(id);
    await calculations.refresh();
    await refreshContext();
    const lastCall = state.turns.flatMap((turn) => turn.calls).at(-1);
    renderResult(lastCall || null);
  } catch (error) {
    if (navigation !== state.navigation) return;
    if (!quiet) toast(error.message);
    viewer.reset(); calculations.reset();
    state.sessionId = null;
    chatModels.newConversation();
    state.turns = [];
    state.runs = [];
    rememberSession(null);
    $("conversation-title").textContent = t("新对话");
  } finally {
    if (navigation === state.navigation) {
      state.loading = false;
      renderConversation();
      renderSessions();
    }
  }
}

function newConversation() {
  if (state.busy || state.loading || chatModels.saving) return;
  chatModels.newConversation();
  state.navigation += 1;
  projectNavigation.clearSelection();
  state.sessionId = null;
  state.turns = [];
  state.runs = [];
  state.pending = null;
  viewer.reset(); calculations.reset();
  $("context-panel").hidden = true;
  rememberSession(null);
  $("conversation-title").textContent = t("新对话");
  $("message-input").value = "";
  uploads.clear();
  renderConversation(false);
  renderSessions();
  renderResult(null);
  closeDrawers();
  $("message-input").focus();
}

async function sendChat(message, context = "", files = uploads.files) {
  if (state.busy || state.loading || chatModels.saving || uploads.uploading || calculations.busy() || !message.trim()) return;
  const selectedModel = chatModels.value;
  if (!state.health?.model_configured) { toast("请先连接并配置模型。"); return; }
  state.busy = true;
  const accountVersion = state.accountVersion;
  const structures = calculations.selection();
  state.pending = { message: message.trim(), context: context.trim(), files, structures };
  renderConversation();
  try {
    if (!state.sessionId) {
      const session = await api("/sessions", { method: "POST", body: JSON.stringify({ title: message.trim().slice(0, 80) }) });
      if (accountVersion !== state.accountVersion) return;
      state.sessionId = session.id;
      $("conversation-title").textContent = session.title;
      rememberSession(session.id);
      await chatModels.saveNewSession(session.id, selectedModel);
      await projectNavigation.assign(session.id);
      if (accountVersion !== state.accountVersion) return;
    }
    const result = await api("/chat", { method: "POST", body: JSON.stringify({
      session_id: state.sessionId, message: message.trim(), context: context.trim() || null,
      file_ids: files.map((file) => file.id), model: selectedModel,
      structure_ids: calculations.selectedIds(),
    }) });
    if (accountVersion !== state.accountVersion) return;
    state.turns.push({ text: message.trim(), context: context.trim(), files, structures, answer: result.answer,
      calls: result.tool_calls, run: { usage: result.usage, context: result.context } });
    state.pending = null;
    calculations.clearSelection();
    $("message-input").value = "";
    $("message-input").style.height = "";
    uploads.clear();
    if (result.tool_calls.length) renderResult(result.tool_calls.at(-1));
    await refreshSessions().catch(() => {});
    await calculations.refresh().catch(() => {});
    await refreshContext();
  } catch (error) {
    if (accountVersion !== state.accountVersion) return;
    state.pending = { message: message.trim(), context: context.trim(), files, structures, error: error.message };
    $("message-input").value = message;
    await refreshSessions().catch(() => {});
    await calculations.refresh().catch(() => {});
    await refreshContext();
  } finally {
    if (accountVersion === state.accountVersion) {
      state.busy = false;
      renderConversation();
      $("message-input").focus();
    }
  }
}

function metric(label, value, unit = "") {
  return `<div class="metric"><small>${escapeHtml(label)}</small><strong>${escapeHtml(metricValue(value))}${unit ? `<span>${escapeHtml(unit)}</span>` : ""}</strong></div>`;
}

const emptyResult = $("result-panel").innerHTML;
function renderResult(call) {
  if (!call) {
    $("result-panel").innerHTML = emptyResult;
    $("result-badge").textContent = "暂无记录";
    $("result-section").open = false;
    return;
  }
  if (!call.success) {
    $("result-section").open = true;
    $("result-panel").innerHTML = `<div class="tool-failure">${escapeHtml(call.result?.message || "计算未完成，请检查输入的分子结构。")}</div>`;
    $("result-badge").textContent = "请检查输入";
    return;
  }
  $("result-section").open = true;
  const result = call.result;
  $("result-badge").textContent = "已计算";
  let body;
  if (call.name === "molecular_properties") {
    body = `<div class="calculation-title"><strong>${escapeHtml(result.formula)}</strong><span>分子属性</span></div><div class="canonical-smiles">${escapeHtml(result.canonical_smiles)}</div><div class="metric-grid">${metric("分子量", result.molecular_weight_g_mol, "g/mol")}${metric("Crippen LogP", result.logp_rdkit_crippen)}${metric("TPSA", result.tpsa_angstrom_squared, "Å²")}${metric("精确质量", result.exact_mass_da, "Da")}${metric("氢键供体", result.hydrogen_bond_donors)}${metric("氢键受体", result.hydrogen_bond_acceptors)}${metric("可旋转键", result.rotatable_bonds)}${metric("环数量", result.ring_count)}</div>`;
  } else if (call.name === "molecular_similarity") {
    const score = Number(result.tanimoto_similarity);
    const percent = Number.isFinite(score) ? Math.max(0, Math.min(100, score * 100)) : 0;
    body = `<div class="calculation-title"><strong>结构相似度</strong><span>TANIMOTO</span></div><div class="canonical-smiles">${escapeHtml(result.canonical_smiles_a)} ↔ ${escapeHtml(result.canonical_smiles_b)}</div><div class="similarity-score"><strong>${escapeHtml(metricValue(score, 6))}</strong><div class="score-track"><span style="width:${percent}%"></span></div><small>0 无指纹重叠 · 1 指纹相同</small></div><div class="result-source">Morgan 指纹 · radius=2 · 2048 bits<br>结构相似度不代表生物活性。</div>`;
  } else {
    body = `<div class="calculation-title"><strong>${escapeHtml(call.name)}</strong></div>`;
  }
  $("result-panel").innerHTML = `<div class="calculation-card">${body}<div class="result-source">${escapeHtml(result.source || "")}</div><details class="raw-result"><summary>查看参数与完整结果</summary><pre>${escapeHtml(JSON.stringify({ arguments: call.arguments, result }, null, 2))}</pre></details></div>`;
}

async function runLocalTool(name, form) {
  const accountVersion = state.accountVersion;
  const button = form.querySelector("button[type=submit]");
  if (button.disabled) return;
  const arguments_ = Object.fromEntries([...new FormData(form)].map(([key, value]) => [key, value.trim()]));
  if (Object.values(arguments_).some((value) => !value)) { toast("请输入有效的 SMILES。"); return; }
  const buttonContent = button.innerHTML;
  button.disabled = true;
  button.textContent = "正在计算…";
  try {
    const call = await api(`/tools/${name}/invoke`, { method: "POST", body: JSON.stringify({ arguments: arguments_ }) });
    if (accountVersion !== state.accountVersion) return;
    renderResult(call);
    toast("计算完成，结果已显示在研究面板。");
  } catch (error) {
    if (accountVersion !== state.accountVersion) return;
    renderResult({ success: false, result: { message: error.message } });
    toast(error.message);
  } finally {
    button.innerHTML = buttonContent;
    button.disabled = false;
  }
}

$("chat-form").addEventListener("submit", (event) => {
  event.preventDefault();
  sendChat($("message-input").value);
});
$("message-input").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
    event.preventDefault();
    $("chat-form").requestSubmit();
  }
});
$("message-input").addEventListener("input", () => {
  $("message-input").style.height = "auto";
  $("message-input").style.height = `${Math.min($("message-input").scrollHeight, 180)}px`;
});
$("new-chat").addEventListener("click", newConversation);
$("session-list").addEventListener("click", (event) => {
  const button = event.target.closest("[data-session]");
  if (button) selectSession(button.dataset.session);
});
$("refresh-sessions").addEventListener("click", () => refreshSessions().catch((error) => toast(error.message)));
$("load-more").addEventListener("click", async () => {
  $("load-more").disabled = true;
  try { await refreshSessions(true); } catch (error) { toast(error.message); }
  finally { $("load-more").disabled = false; }
});
$("conversation").addEventListener("click", async (event) => {
  if (event.target.closest("[data-task-panel]")) selectInspectorTab("calculation");
  const resultButton = event.target.closest("[data-result]");
  if (resultButton) {
    const call = state.results.get(resultButton.dataset.result);
    if (call.name.startsWith("calculation_")) selectInspectorTab("calculation");
    else { renderResult(call); selectInspectorTab("tools"); }
  }
  const copyButton = event.target.closest("[data-copy]");
  if (copyButton) {
    try { await navigator.clipboard.writeText(state.turns[Number(copyButton.dataset.copy)].answer); toast("回答已复制。"); }
    catch { toast("复制不可用，请选中文字后手动复制。"); }
  }
  if (event.target.closest("#retry-message") && state.pending && !state.busy) {
    sendChat(state.pending.message, state.pending.context, state.pending.files);
  }
});
$("properties-form").addEventListener("submit", (event) => { event.preventDefault(); runLocalTool("molecular_properties", event.currentTarget); });
$("similarity-form").addEventListener("submit", (event) => { event.preventDefault(); runLocalTool("molecular_similarity", event.currentTarget); });
function setSidebarCollapsed(collapsed, reveal = false) {
  document.body.classList.toggle("sidebar-collapsed", collapsed);
  $("sidebar-collapse").setAttribute("aria-expanded", String(!collapsed));
  $("sidebar-collapse").setAttribute("aria-label", collapsed ? "展开侧栏" : "折叠侧栏");
  $("sidebar-collapse").title = collapsed ? "展开侧栏" : "折叠侧栏";
  try { localStorage.setItem("chemistry.sidebarCollapsed", String(collapsed)); } catch { /* Optional. */ }
  if (reveal && innerWidth <= 760) setDrawer("sidebar", !collapsed);
  syncDrawerAccessibility();
}
try { setSidebarCollapsed(localStorage.getItem("chemistry.sidebarCollapsed") === "true"); } catch { /* Default expanded. */ }
$("sidebar-collapse").addEventListener("click", () => setSidebarCollapsed(!document.body.classList.contains("sidebar-collapsed"), true));
$("rail-projects").addEventListener("click", () => { setSidebarCollapsed(false, true); $("new-project").focus(); });
$("menu-toggle").addEventListener("click", () => { setDrawer("inspector", false); setDrawer("sidebar", !document.body.classList.contains("sidebar-open")); });
$("tools-toggle").addEventListener("click", () => { setDrawer("sidebar", false); setDrawer("inspector", !document.body.classList.contains("inspector-open")); });
$("inspector-close").addEventListener("click", () => setDrawer("inspector", false));
$("screen-overlay").addEventListener("click", closeDrawers);
document.addEventListener("keydown", (event) => {
  if ($("project-dialog").open) return;
  if (event.key === "Escape") { closeDrawers(); setInspectorOpen(false); }
  if (event.key !== "Tab") return;
  const openPanel = document.body.classList.contains("sidebar-open") ? $("sidebar") : null;
  if (!openPanel) return;
  const focusable = [...openPanel.querySelectorAll("a, button:not(:disabled), input, textarea, summary")]
    .filter((element) => element.getClientRects().length && !element.disabled);
  const first = focusable[0], last = focusable.at(-1);
  if (event.shiftKey && (document.activeElement === first || !openPanel.contains(document.activeElement))) {
    event.preventDefault(); last?.focus();
  } else if (!event.shiftKey && (document.activeElement === last || !openPanel.contains(document.activeElement))) {
    event.preventDefault(); first?.focus();
  }
});
window.addEventListener("resize", () => {
  if (innerWidth > 760) closeDrawers();
  syncDrawerAccessibility();
});

function showLogin(message = "") {
  viewer.reset(); calculations.reset(); notifications.enabled(false);
  $("context-panel").hidden = true;
  state.accountVersion += 1;
  state.navigation += 1;
  rememberSession(null);
  state.sessionId = null;
  state.sessions = [];
  state.turns = [];
  state.runs = [];
  state.pending = null;
  state.results.clear();
  state.auth.user = null;
  chatModels.reset();
  projectNavigation.reset();

  state.busy = false;
  state.loading = false;
  $("conversation").replaceChildren();
  $("session-list").replaceChildren();
  $("conversation-title").textContent = t("新对话");
  $("message-input").value = "";
  uploads.clear();
  $("properties-form").reset();
  $("similarity-form").reset();
  renderResult(null);
  closeDrawers();
  $("login-screen").hidden = false;
  document.body.classList.remove("auth-checking");
  $("login-error").textContent = message;
  $("login-error").hidden = !message;
  $("login-password").value = "";
  syncDrawerAccessibility();
  $("login-username").focus();
}

async function startWorkspace() {
  $("login-screen").hidden = true;
  document.body.classList.remove("auth-checking");
  syncDrawerAccessibility();
  await Promise.all([refreshHealth(), refreshSessions().catch((error) => {
    $("session-list").innerHTML = '<p class="sidebar-empty">暂时无法读取会话，请点击刷新。</p>';
    toast(error.message);
  })]);
  let id = new URL(location.href).searchParams.get("session");
  const storageKey = state.auth.user ? `chemistry.session.${state.auth.user.id}` : "chemistry.session";
  try { id ||= localStorage.getItem(storageKey); } catch { /* Optional persistence. */ }
  if (state.health?.model_configured) await chatModels.refresh();
  if (id) await selectSession(id, true);
  else { $("conversation-title").textContent = t("新对话"); updateControls(); }
}

async function initialize() {
  try {
    state.auth = await api("/auth/status");
    if (state.auth.enabled && !state.auth.user) showLogin();
    else await startWorkspace();
  } catch {
    showLogin("暂时无法连接服务，请稍后刷新页面重试。");
  }
}

$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("login-submit").disabled = true;
  $("login-error").hidden = true;
  try {
    const result = await api("/auth/login", { method: "POST", body: JSON.stringify({
      username: $("login-username").value, password: $("login-password").value,
    }) });
    state.auth = { enabled: true, user: result.user };
    $("login-password").value = "";
    await startWorkspace();
  } catch (error) {
    $("login-error").textContent = error.message;
    $("login-error").hidden = false;
  } finally {
    $("login-submit").disabled = false;
  }
});

window.addEventListener("languagechange", () => {
  if (!state.sessionId) $("conversation-title").textContent = t("新对话");
});
try {
  const savedInspector = localStorage.getItem("chemistry.inspectorOpen");
  setInspectorOpen(savedInspector === null ? innerWidth > 1120 : savedInspector === "true", { focus:false, persist:false });
} catch { setInspectorOpen(innerWidth > 1120, { focus:false, persist:false }); }
initialize();
setInterval(() => { if ($("login-screen").hidden) refreshHealth(); }, 30000);

async function refreshContext() {
  const id = state.sessionId;
  if (!id) { $("context-panel").hidden = true; return; }
  try {
    const info = await api(`/sessions/${encodeURIComponent(id)}/context`);
    if (id !== state.sessionId) return;
    $("context-panel").hidden = false;
    $("context-status").textContent = `会话记忆 · 已摘要 ${info.covered_messages} / ${info.original_messages} 条消息`;
    $("context-budget").textContent = `估算输入 ${info.estimated_input_tokens.toLocaleString()} / ${info.input_budget_tokens.toLocaleString()} tokens（${info.tokenizer}）；自动触发 ${info.trigger_tokens.toLocaleString()}，保留最近 ${info.recent_turns} 轮。`;
    $("context-summary").textContent = info.summary || "尚未生成历史摘要。短对话可直接保留原文。";
  } catch (error) { toast(error.message); }
}
$("compact-context").onclick = async () => {
  if (!state.sessionId || state.busy || state.loading || calculations.busy()) return;
  state.busy = true; updateControls();
  try {
    const result = await api(`/sessions/${encodeURIComponent(state.sessionId)}/context/compact`, { method: "POST" });
    await refreshContext();
    toast(result.context.summary_calls ? "历史摘要已更新，原文和计算产物完整保留。" : "暂无需要进一步压缩的历史，保留最近对话。 ");
  } catch (error) { toast(error.message); }
  finally { state.busy = false; updateControls(); }
};

$("structure-upload").addEventListener("click", () => {
  $("composer-upload-menu").hidden = true;
  $("composer-add").setAttribute("aria-expanded", "false");
});
