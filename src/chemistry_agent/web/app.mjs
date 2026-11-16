import { externalContextView } from "./external-context.mjs";
import {createWorkspaceFiles} from "./workspace-files.mjs";
import {userQuestions} from './user-questions.mjs';
import {visibleReference, activity, activityOpen, activitySteps, answerBox, answerFooter, liveAnswerFooter, initAnswerControls, initDeferredActivities} from "./answer-ui.mjs";
import {liveAnswer} from "./live-answer.mjs";
import {createProgressFeed} from './progress-feed.mjs';
import { initSettingsDialog } from "./settings-dialog.mjs";
import {initWorkspaceControls} from './workspace-controls.mjs';
import {resultFigures} from './result-figures.mjs';
import { initInspectorTabs } from "./inspector-tabs.mjs";
import { createStructureBridge, structureButton, inlineStructureName, isStructureName, getFileBlob } from "./structure-links.mjs";
import { createCalculations } from "./calculations.mjs?v=20261102-general-plans";
import { createNotifications } from "./notifications.mjs";
import { createChatModelPicker } from "./chat-models.mjs";
import { t } from "./i18n.mjs";
import { createFileUploads, renderFiles, contextFiles } from "./files.mjs";
import { createProjectNavigation } from "./projects.mjs";
import { TOOL_LABELS, buildTurns, createMarkdownRenderer, escapeHtml, metricValue } from "./core.mjs";

const $ = (id) => document.getElementById(id);
const icon = (name, className = "") => `<svg class="icon ${className}" aria-hidden="true"><use href="/assets/ui-icons.svg#i-${name}"/></svg>`;
const markdown = globalThis.markdownit ? createMarkdownRenderer(globalThis.markdownit) : {
  render: (text) => `<p>${escapeHtml(text).replace(/\n/g, "<br>")}</p>`,
};
const state = {
  sessions: [], sessionId: null, turns: [], runs: [], pending: null, health: null,
  outbox: [], chatSignature: "",
  busy: false, loading: false, metadataLoading: false, hasMore: false, navigation: 0,
  auth: { enabled: false, user: null }, accountVersion: 0,
};
const setDockMaximized = initWorkspaceControls({closeInspector:()=>setInspectorOpen(false)});
initAnswerControls({root:document.body,answer:i=>state.turns[i]?.answer,toast});
initDeferredActivities({root:$("conversation"), resolve:id=>state.turns.find((turn,index)=>String(turn.run?.id || index)===id)});

const chatPositions=new Map();
let restorePosition=false;
let toastTimer;
let chatModels;
let inspectorSession = null;
let contextGeneration = 0;
let nativeContext = null;
let contextSignature = "";
let contextRequest = null;
const nativeContextSynced = new Set();
let contextPolling = false;
const notifications = createNotifications({ api, selectSession });
const inspectorTabs = initInspectorTabs(open => {
  if (open) setDrawer("sidebar", false);
  setInspectorOpen(open, {focus:false});
});
const selectInspectorTab = inspectorTabs.select;
const viewer = createStructureBridge({selectTab:selectInspectorTab, toast});
async function syncReport(id,event) {
    if (state.busy || state.loading || state.turns.some(t=>t.event===event)) return;
    const generation = state.navigation;
    const [session,runs] = await Promise.all([api("/sessions/"+id),api("/sessions/"+id+"/runs?limit=100&compact=true")]);
    if (!state.busy && !state.loading && generation===state.navigation && state.sessionId===id) {
      state.runs = runs;
      state.turns = buildTurns(session.messages,runs);
      renderConversation(false);
    }
  }
const progressFeed=createProgressFeed({
  api,getSession:()=>state.sessionId,markdown,
  async onFinal(id,key) {
    await syncReport(id,key);
    if(id!==state.sessionId) return false;
    renderConversation(false);
    return state.turns.some(turn=>turn.event===key);
  }
});
const calculations = createCalculations({
  syncReport, refreshProgress:id=>progressFeed.refresh(id),
  onPlans:(id,plans)=>progressFeed.setPlans(id,plans),
  reviseStep(id,title,planRevision) {
    const field = $("message-input");
    if (innerWidth<=767) setInspectorOpen(false);
    state.revision = {id,title,planRevision};
    $("revision-chip").hidden=false;
    $("revision-chip").textContent="修改："+title;
    $("revision-chip").insertAdjacentHTML("beforeend", icon("close"));
    field.focus();
  },
  viewer, chatFiles: () => [...state.turns.flatMap(turn => turn.files?.length ? turn.files : contextFiles(turn.context)), ...uploads.files],
  api, getSession: () => state.sessionId,
  isBusy: () => state.busy || state.loading || chatModels?.saving || uploads.uploading,
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
  $("memory-entry").href = id ? `/memory?session=${encodeURIComponent(id)}` : "/memory";
  const storageKey = state.auth.user ? `chemistry.session.${state.auth.user.id}` : "chemistry.session";
  try {
    if (id) localStorage.setItem(storageKey, id);
    else localStorage.removeItem(storageKey);
  } catch { /* Storage can be unavailable in a private browser. */ }
  if (history.state?.workspaceSettings) return;
  const url = new URL(location.href);
  if (id) url.searchParams.set("session", id);
  else url.searchParams.delete("session");
  history.replaceState(null, "", url);
}

function syncDrawerAccessibility() {
  const loginRequired = !$("login-screen").hidden;
  const sidebarHidden = loginRequired || (innerWidth <= 767 && !document.body.classList.contains("sidebar-open") && !document.body.classList.contains("sidebar-collapsed"));
  const inspectorHidden = loginRequired || !document.body.classList.contains("inspector-open");
  const mainHidden = loginRequired || (innerWidth <= 767 && !inspectorHidden);
  document.querySelector(".main-panel").inert = mainHidden;
  document.querySelector(".main-panel").setAttribute("aria-hidden", String(mainHidden));
  $("sidebar").inert = sidebarHidden;
  $("inspector").inert = inspectorHidden;
  $("sidebar").setAttribute("aria-hidden", String(sidebarHidden));
  $("inspector").setAttribute("aria-hidden", String(inspectorHidden));
}

function inspectorKey(id) {
  return "chemistry.inspector." + (state.auth.user?.id || "local") + "." + (id || "draft");
}
function saveInspector(id) {
  const open = document.body.classList.contains("inspector-open");
  try { localStorage.setItem(inspectorKey(id), JSON.stringify({...inspectorTabs.snapshot(), open})); } catch { /* Optional. */ }
}
function adoptInspector(id) {
  inspectorSession = id;
  saveInspector(id);
  try { localStorage.removeItem(inspectorKey(null)); } catch { /* Optional. */ }
}
function restoreInspector(id, fresh = false) {
  inspectorSession = id;
  let saved;
  try {
    if (fresh) localStorage.removeItem(inspectorKey(id));
    else saved = JSON.parse(localStorage.getItem(inspectorKey(id)));
  } catch { /* Use the default dock. */ }
  setDockMaximized(false);
  inspectorTabs.restore(saved);
  setInspectorOpen(saved?.open === true, {focus:false, persist:false});
}
function setInspectorOpen(open, { focus = true, persist = true } = {}) {
  const wasFocused = $("inspector").contains(document.activeElement);
  if (open) inspectorTabs.loadActive();
  document.body.classList.toggle("inspector-open", open);
  if (!open) setDockMaximized(false);
  $("tools-toggle").disabled = open;
  $("tools-toggle").setAttribute("aria-expanded", String(open));
  $("inspector-close").setAttribute("aria-expanded", String(open));
  if (persist) saveInspector(inspectorSession);
  syncDrawerAccessibility();
  if (focus && open) inspectorTabs.focusActive();
  else if (focus && wasFocused) $("tools-toggle").focus({preventScroll:true});
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
  if (innerWidth <= 767) setInspectorOpen(false, { focus:false, persist:false });
}

function updateControls() {
  const locked = state.busy || state.loading || chatModels?.saving || calculations.busy();
  const external = state.health?.agent_backend && state.health.agent_backend !== "builtin";
  const view = nativeContext ? externalContextView(nativeContext) : null;
  $("compact-context").hidden = !state.sessionId || (external && !view?.manual);
  $("compact-context").disabled = locked || !state.sessionId ||
    (external ? !view || view.disabled : !state.health?.model_configured);
  $("agent-picker").disabled = Boolean(locked || state.metadataLoading);
  $("permission-picker").disabled = Boolean(locked || state.metadataLoading);
  $("message-input").disabled = state.loading;
  $("send-button").disabled = locked || state.metadataLoading || uploads.uploading || !state.health?.model_configured;
  $("composer-add").disabled = locked || uploads.uploading;
  $("send-button").setAttribute("aria-label", state.busy ? "正在生成回答" : "发送消息");
  $("new-chat").disabled = locked;
  $("new-chat-list").disabled = locked;
  $("new-project").disabled = locked;
  $("draft-project").disabled = locked || Boolean(state.sessionId);
  document.querySelectorAll("[data-new-in]").forEach((button) => { button.disabled = locked; });
  document.querySelectorAll(".session-button,.suggestion-card").forEach((button) => { button.disabled = locked; });
  $("chat-form").setAttribute("aria-busy", String(state.busy));
}

async function refreshHealth() {
  const healthSession = state.sessionId;
  try {
    const health = await api("/health" + (healthSession ? `?session_id=${healthSession}` : ""));
    if (healthSession !== state.sessionId) return;
    state.health = chatModels?.adaptHealth(health) || health;
    $("shell-options").replaceChildren(...(state.health.ssh_hosts || []).map(host => {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = "远程终端 · " + host;
      button.title = "使用 " + host + " 的终端";
      button.onclick = () => {
        const field = $("message-input");
        field.value = "/shell " + host + "\n" + field.value.replace(/^\/shell[ \t]+[^\n]*\n?/, "");
        field.dispatchEvent(new Event("input", {bubbles:true}));
        $("composer-upload-menu").hidden = true;
        $("composer-add").setAttribute("aria-expanded", "false");
        field.focus(); field.setSelectionRange(field.value.length, field.value.length);
      };
      return button;
    }));
    calculations.enabled(Boolean(state.health.automation_enabled));
    notifications.enabled(Boolean(state.health.automation_enabled));
    chatModels?.render();
    const names = { "deepseek-flash": "DeepSeek Flash", "deepseek-v4-pro": "DeepSeek Pro" };
    $("model-label").textContent = state.health.model_configured
      ? `${names[state.health.model] || state.health.model} · ${state.health.provider === "chatgpt" ? "已登录" : "已配置"}` : "模型未配置";
    $("model-status").classList.toggle("unavailable", !state.health.model_configured);
  } catch {
    if (healthSession !== state.sessionId) return;
    state.health = null;
    $("model-label").textContent = "服务未连接";
    $("model-status").classList.add("unavailable");
  }
  updateControls();
}

const titledSessions = new Set();
async function updateSessionTitle(id, session, turns) {
  const first = turns.find(turn=>!turn.event && turn.answer);
  if (!first || titledSessions.has(id) || !session.title_pending) return;
  titledSessions.add(id);
  const version=state.accountVersion;
  try {
    const result=await api("/sessions/"+id+"/title",{method:"POST"});
    if (version!==state.accountVersion) return;
    if (state.sessionId===id) $("conversation-title").textContent=result.title;
    await refreshSessions();
  } catch { titledSessions.delete(id); }
}

function sessionButton(session) {
  return `<button class="session-button${session.id === state.sessionId ? " active" : ""}" data-session="${escapeHtml(session.id)}" aria-current="${session.id === state.sessionId ? "page" : "false"}" title="${escapeHtml(session.title)}"><span>${escapeHtml(session.title)}</span></button>`;
}

const projectNavigation = createProjectNavigation({ api, state, selectSession, newConversation, refreshSessions, toast, sessionButton });
chatModels = createChatModelPicker({ api, state, toast, updateControls, refreshHealth });
const uploads = createFileUploads({ state, toast, updateControls, onChange: () => calculations.drawStructures(), onUnauthorized: () => showLogin("登录已过期，请重新登录。") });

function renderSessions() {
  const chats = state.sessions.filter((session) => !projectNavigation.includes(session.id));
  $("session-list").innerHTML = chats.length ? chats.map(sessionButton).join("") : '<p class="sidebar-empty">暂无对话</p>';
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

const workspaceFiles=createWorkspaceFiles({api,session:()=>state.sessionId,viewer,
  attach:file=>{uploads.add(file);$("message-input").focus();},toast});

function renderUser(text, context, files = [], structures = []) {
  const attachedFiles = files.length ? files : contextFiles(context);
  context = visibleReference(context);
  return `<div class="user-message"><div class="user-bubble"><span data-no-i18n>${escapeHtml(text)}</span>${renderFiles(attachedFiles)}${structures.map(s => structureButton("artifact",s.id,s.name+".extxyz","查看 "+s.name)).join("")}${context && !attachedFiles.length ?
    `<details class="attached-reference"><summary>${icon("document")} ${t("附件与上下文")}</summary><pre>${escapeHtml(context)}</pre></details>` : ""}</div></div>`;
}

function artifactLinks(calls) {
  const links = new Map();
  for (const call of calls) {
    if (!call.success) continue;
    const r = call.result || {};
    if (call.name === "calculation_analyze" && r.analysis_id) {
      const base = "/api/automation/chat/" + encodeURIComponent(state.sessionId) + "/analyses/" + encodeURIComponent(r.analysis_id) + "/artifacts";
      links.set(r.analysis_id,resultFigures(base,r.files,r.name || "分析结果"));
    }
    const isStructure = ["build_structure","import_structure","calculation_import_result"].includes(call.name);
    const id = isStructure ? r.id : r.surface_structure_id || r.structure_id;
    if (id) links.set(id, structureButton("artifact",id,(r.name || "structure")+".extxyz","查看"+(r.name || "输入结构")));
    if (r.finished_ok && r.output_structure && r.id) links.set(r.id,structureButton("job",r.id,"optimized.extxyz","查看优化结构"));
    if (/^calculation_(?:plan|prepare|preview|submit|job)/.test(call.name) && !isStructure) links.set("task",`<button type="button" class="plan-entry" data-task-panel>${icon('map')}<span>查看方案</span>${icon('chevron')}</button>`);
  }
  return [...links.values()].join(" ");
}

const questionCard = userQuestions({root:$("conversation"), session:()=>state.sessionId,
  api, sync:()=>syncChat(), toast});
function turnAnswer(turn,index) {
  const ask = turn.calls.find(call=>call.name==="ask_user_question" && call.success && call.result?.status==="awaiting_user");
  if (!ask) return answerBox(markdown.render(turn.answer));
  const active=!state.turns.slice(index+1).some(other=>!other.event) && !state.outbox.length && !state.pending;
  return questionCard(ask.result,active);
}

function renderConversation(scroll = true) {
  const viewport=$("chat-scroll");
  const previousTop=viewport.scrollTop;
  const pinned=viewport.scrollHeight-viewport.clientHeight-previousTop<48;
  const expanded = new Map([...$("conversation").querySelectorAll("[data-activity]")].map(el=>[el.dataset.activity,{open:el.open,running:el.dataset.running==='true'}]));
  const hasConversation = state.turns.length > 0 || state.pending !== null || state.outbox.length > 0;
  $("welcome").hidden = hasConversation || state.loading;
  viewport.closest(".main-panel").classList.toggle("new-session", !$("welcome").hidden);
  $("conversation").hidden = !hasConversation && !state.loading;
  if (state.loading) {
    streamed.close();
    $("conversation").innerHTML = '<div class="thinking-indicator">正在打开这段研究对话…</div>';
    return;
  }
  progressFeed.setHistory(state.sessionId,state.turns);
  $("conversation").innerHTML = state.turns.map((turn, index) =>
    turn.event && !progressFeed.isFinal(turn.event) ? "" : turn.event ? `<article class="turn"><div class="assistant-message"><div class="assistant-body"><div data-plan-report="${escapeHtml(turn.event)}"></div>${answerBox(markdown.render(turn.answer))}${answerFooter(index)}</div></div></article>` : `<article class="turn">${turn.background ? '' : renderUser(turn.calls.some(c => c.name === "calculation_submit_confirmed") ? turn.text.replace(/^确认提交预览 [0-9a-f-]{36}/i,"确认提交") : turn.text, turn.context, turn.files, turn.structures)}<div class="assistant-message"><div class="assistant-body">${activity({id:turn.run?.id || index,start:turn.run?.started_at,end:turn.run?.finished_at,calls:turn.calls,steps:turn.steps,running:turn.run?.status==="running",deferred:turn.run?.status!=="running"})}${turnAnswer(turn,index)}${artifactLinks(turn.calls)}<div data-generated-run="${escapeHtml(turn.run?.id || "")}"></div>${turn.answer ? answerFooter(index) : ''}</div></div></article>`

  ).join("");
  calculations.drawReportFigures();
  for (const item of state.outbox) {
    const article = document.createElement("article");
    article.className = "turn";
    const failed = ["failed","uncertain"].includes(item.status);
    const label = failed ? item.error || "处理失败" : item.status==="queued" ? "等待上一条回复" : "";
    article.innerHTML = renderUser(item.body.message, item.body.context || "") +
      `<div class="assistant-message"><div class="assistant-body">${activity({id:item.run_id || item.id,start:item.started_at || (item.status==='running'?item.created_at:null),end:item.finished_at,calls:item.tools || [],steps:item.steps || [],running:item.status==='running',draftRequest:item.id})}${label?`<div class="${failed?'message-error':'thinking-indicator'}">${escapeHtml(label)}</div>`:''}${item.status === "failed" ? `<button class="retry-button" data-retry-request="${escapeHtml(item.id)}">重试这条消息</button>` : ""}${liveAnswerFooter(item.id)}</div></div>`;
    $("conversation").append(article);
  }
  for (const el of $("conversation").querySelectorAll("[data-activity]")) el.open=activityOpen(el.dataset.running==='true',expanded.get(el.dataset.activity));
  streamed.follow(state.sessionId, (state.outbox.find(item => item.status === "running") || state.outbox.find(item => item.status === "queued"))?.id);
  if (state.pending) {
    const pending = state.pending;
    const article = document.createElement("article");
    article.className = "turn";
    article.innerHTML = `${renderUser(pending.message, pending.context, pending.files, pending.structures)}<div class="assistant-message"><div class="assistant-body">${pending.error ? `<div class="message-error">${escapeHtml(pending.error)}<br><button class="retry-button" id="retry-message">${icon("refresh")}重试这条消息</button></div>` : '<div class="thinking-indicator"><span></span><span></span><span></span>正在分析问题…</div>'}</div></div>`;
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
  workspaceFiles.draw();
  if (restorePosition && state.sessionId) {
    viewport.scrollTop=chatPositions.get(state.sessionId) ?? viewport.scrollHeight;
  } else {
    viewport.scrollTop=pinned ? viewport.scrollHeight : previousTop;
  }
  updateControls();
}

async function selectSession(id, quiet = false) {
  const openedAt = performance.now();
  if (state.busy) return;
  if(state.sessionId && !state.loading) chatPositions.set(state.sessionId,$("chat-scroll").scrollTop);
  restorePosition=true;
  saveDraft();
  if (id !== state.sessionId) { resetContextMeter("正在读取上下文…"); progressFeed.reset(id); clearRevision(); state.sessionId = null; state.turns = []; state.runs = []; state.outbox = []; state.chatSignature = ""; viewer.reset(); calculations.reset(); }
  workspaceFiles.reset();
  uploads.select(id);
  calculations.clearSelection();
  const navigation = ++state.navigation;
  state.sessionId = id;
  state.health = null;
  state.metadataLoading = true;
  state.loading = true;
  state.pending = null;
  renderConversation(false);
  setDrawer("sidebar", false);
  if (inspectorSession !== id) restoreInspector(id);
  // Loading model catalogs must never gate reading a saved conversation.
  void Promise.allSettled([chatModels.loadSession(id), refreshHealth()]).then(() => {
    if (navigation !== state.navigation || state.sessionId !== id) return;
    state.metadataLoading = false;
    $("conversation").dataset.metadataLoadedMs = String(Math.round(performance.now() - openedAt));
    updateControls();
    if (!state.loading) void calculations.refresh().catch(() => {});
    void chatModels.refresh(false);
  });
  try {
    // Message history is authoritative. All tool details can be reconstructed from it.
    const [session, runs] = await Promise.all([
      api(`/sessions/${encodeURIComponent(id)}`),
      api(`/sessions/${encodeURIComponent(id)}/runs?limit=100&compact=true`).catch(() => []),
    ]);
    if (navigation !== state.navigation) return;
    $("conversation").dataset.historyLoadedMs = String(Math.round(performance.now() - openedAt));
    state.runs = runs;
    state.turns = buildTurns(session.messages, runs);
    state.loadedAt = session.updated_at;
    $("conversation-title").textContent = session.title;
    void updateSessionTitle(id,session,state.turns);
    rememberSession(id);
    const lastCall = state.turns.flatMap((turn) => turn.calls).at(-1);
    renderResult(lastCall || null);
  } catch (error) {
    if (navigation !== state.navigation) return;
    if (!quiet) toast(error.message);
    viewer.reset(); calculations.reset();
    state.sessionId = null;
    state.metadataLoading = false;
    chatModels.newConversation();
    state.turns = [];
    state.runs = [];
    rememberSession(null);
    workspaceFiles.reset();
    uploads.select(null); restoreInspector(null);
    $("conversation-title").textContent = t("新对话");
  } finally {
    if (navigation === state.navigation) {
      state.loading = false;
      restoreDraft();
      renderConversation();
      restorePosition=false;
      performance.clearMeasures("chem:session:ready");
      performance.measure("chem:session:ready", {start:openedAt});
      $("conversation").dataset.readyMs = String(Math.round(performance.now() - openedAt));
      renderSessions();
      void syncChat();
      if (!state.metadataLoading) void calculations.refresh().catch(() => {});
      void workspaceFiles.refresh();
      void refreshContext();
    }
  }
}

function clearRevision() {
  state.revision = null;
  $("revision-chip").hidden = true;
}
$("revision-chip").addEventListener("click",clearRevision);

function newConversation() {
  if(state.sessionId) chatPositions.set(state.sessionId,$("chat-scroll").scrollTop);
  if (state.busy || state.loading || chatModels.saving) return;
  saveDraft();
  chatModels.newConversation();
  state.navigation += 1;
  clearRevision(); projectNavigation.clearSelection();
  state.sessionId = null;
  state.metadataLoading = false;
  void refreshHealth();
  state.turns = [];
  state.runs = [];
  state.pending = null; state.outbox = []; state.chatSignature = "";
  viewer.reset(); calculations.reset();
  resetContextMeter();
  rememberSession(null);
  restoreInspector(null, true);
  $("conversation-title").textContent = t("新对话");
  $("message-input").value = "";
  uploads.select(null);
  renderConversation(false);
  renderSessions();
  renderResult(null);
  closeDrawers();
  $("message-input").focus();
}

function draftKey() {
  return `chemistry.draft.${state.auth.user?.id || "local"}.${state.sessionId || "new"}`;
}

function saveDraft(pending = state.pending?.request || null) {
  try {
    localStorage.setItem(draftKey(), JSON.stringify({text:$("message-input").value, pending}));
  } catch { /* The server receipt still persists every accepted message. */ }
}

function restoreDraft() {
  state.outbox = []; state.chatSignature = "";
  try {
    const draft = JSON.parse(localStorage.getItem(draftKey()) || "{}");
    $("message-input").value = draft.text || "";
    state.pending = draft.pending ? {
      message:draft.pending.message, context:draft.pending.context || "",
      files:[], structures:[], request:draft.pending,
      error:"发送状态未确认，可重试。",
    } : null;
  } catch { state.pending = null; }
}

const streamed = liveAnswer({
  render(request, text, steps) {
    const el = document.querySelector(`[data-live-request="${CSS.escape(request)}"]`);
    if (!el) return;
    const scroll = $("chat-scroll");
    const pinned = scroll.scrollHeight - scroll.clientHeight - scroll.scrollTop < 56;
    const activity = el.closest('[data-activity]');
    if (steps && activity.dataset.steps !== JSON.stringify(steps)) {
      activity.dataset.steps = JSON.stringify(steps);
      activity.querySelector('[data-activity-steps]').innerHTML = activitySteps(steps);
    }
    if (el.dataset.preview !== text) {
      el.dataset.preview = text;
      el.innerHTML = text ? (markdown.renderPartial?.(text) ?? markdown.render(text)) : "";
    }
    el.parentElement.hidden = !text;
    const footer=el.closest('.assistant-body')?.querySelector(`[data-live-footer="${CSS.escape(request)}"]`);
    if(footer) footer.hidden=!text;
    activity.querySelector('[data-activity-placeholder]').hidden = Boolean(text || activity.querySelector('[data-activity-steps]').childElementCount);
    if (pinned) scroll.scrollTop = scroll.scrollHeight;
  },
  settle: () => void syncChat(),
});
let syncingChat = false;
async function syncChat() {
  const id = state.sessionId, generation = state.navigation;
  if (!id || state.loading || state.busy || syncingChat) return;
  syncingChat = true;
  try {
    const receipt = await api(`/sessions/${id}/chat-requests?compact=true`);
    if (id !== state.sessionId || generation !== state.navigation || state.busy) return;
    const signature = JSON.stringify([receipt.updated_at, receipt.active_run_id,
      receipt.requests.map(item=>[item.id,item.status,item.error,item.tools,item.steps])]);
    if (state.pending?.request && receipt.requests.some(item=>item.id===state.pending.request.request_id)) {
      if ($("message-input").value.trim() === state.pending.message) $("message-input").value = "";
      state.pending = null;
      saveDraft();
    }
    if (signature === state.chatSignature) return;
    let runs = state.runs;
    let transcriptChanged = false;
    if (receipt.updated_at !== state.loadedAt ||
        state.runs.some(run=>run.status==='running' && run.id!==receipt.active_run_id)) {
      const [session, latest] = await Promise.all([
        api(`/sessions/${id}`), api(`/sessions/${id}/runs?limit=100&compact=true`),
      ]);
      if (id !== state.sessionId || generation !== state.navigation || state.busy) return;
      transcriptChanged = true;
      runs = latest;
      state.runs = runs;
      state.turns = buildTurns(session.messages, runs);
      state.loadedAt = session.updated_at;
      void updateSessionTitle(id,session,state.turns);
      void calculations.refresh().catch(() => {});
      void workspaceFiles.refresh();
      void refreshContext();
    }
    const outbox = receipt.requests.filter(item=>item.status!=="completed" &&
      !runs.some(run=>run.id===item.run_id && run.status==="completed"));
    const outboxChanged = JSON.stringify(outbox) !== JSON.stringify(state.outbox);
    state.outbox = outbox;
    state.chatSignature = signature;
    if (transcriptChanged || outboxChanged) renderConversation(false);
  } catch { /* Keep saved messages and drafts visible while disconnected. */ }
  finally { syncingChat = false; }
}
setInterval(syncChat, 2000);
window.addEventListener("focus", syncChat);
window.addEventListener("pagehide", () => saveDraft());

async function sendChat(message, context = "", files = uploads.files, retryRequest = null) {
  if (state.busy || state.loading || state.metadataLoading || chatModels.saving || uploads.uploading || !message.trim()) return;
  const selectedModel = chatModels.value;
  if (!state.health?.model_configured) { toast("请先连接并配置模型。"); return; }
  state.busy = true;
  const accountVersion = state.accountVersion;
  const structures = calculations.selection();
  let outbound = retryRequest || state.pending?.request;
  state.pending = { message:message.trim(), context:context.trim(), files, structures };
  renderConversation();
  try {
    if (!state.sessionId) {
      const session = await api("/sessions", { method:"POST", body:JSON.stringify({}) });
      if (accountVersion !== state.accountVersion) return;
      state.sessionId = session.id;
      $("conversation-title").textContent = session.title;
      rememberSession(session.id);
      adoptInspector(session.id);
      await chatModels.saveNewSession(session.id, selectedModel);
      await projectNavigation.assign(session.id);
    }
    if (!outbound || outbound.message !== message.trim() || outbound.context !== (context.trim() || null)) {
      outbound = {
        request_id:crypto.randomUUID(), session_id:state.sessionId,
        message:message.trim(), context:context.trim() || null,
        file_ids:files.map(file=>file.id), model:selectedModel,
        structure_ids:calculations.selectedIds(),
      };
    }
    state.pending.request = outbound;
    saveDraft(outbound);
    const receipt = await api("/chat/requests" + (retryRequest ? "?retry=true" : ""), {
      method:"POST", body:JSON.stringify(outbound),
    });
    if (accountVersion !== state.accountVersion) return;
    state.pending = null;
    if (receipt && ["queued","running"].includes(receipt.status)) {
      state.outbox = [...state.outbox.filter(item => item.id !== receipt.id), {...receipt, body:outbound}];
      renderConversation(false);
    }
    calculations.clearSelection();
    clearRevision();
    if ($("message-input").value.trim() === message.trim()) {
      $("message-input").value = "";
      $("message-input").style.height = "";
    }
    uploads.clear();
    uploads.select(state.sessionId);
    saveDraft();
    await refreshSessions().catch(()=>{});
  } catch (error) {
    if (accountVersion !== state.accountVersion) return;
    state.pending = { message:message.trim(), context:context.trim(), files, structures, request:outbound, error:error.message };
    saveDraft(outbound);
  } finally {
    if (accountVersion === state.accountVersion) {
      state.busy = false;
      await syncChat();
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
  $("result-section").hidden = !call;
  if (!call) {
    $("result-panel").innerHTML = emptyResult;
    $("result-badge").textContent = "暂无记录";
    $("result-section").open = false;
    return;
  }
  if (!call.success) {
    $("result-section").open = true;
    $("result-panel").innerHTML = `<div class="tool-failure">${escapeHtml(call.result?.message || "计算未完成，请检查输入的分子结构。")}</div>`;
    $("result-badge").textContent = "未完成";
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
    body = `<div class="calculation-title"><strong>结构相似度</strong><span>TANIMOTO</span></div><div class="canonical-smiles">${escapeHtml(result.canonical_smiles_a)} ↔ ${escapeHtml(result.canonical_smiles_b)}</div><div class="similarity-score"><strong>${escapeHtml(metricValue(score, 6))}</strong><div class="score-track"><span style="width:${percent}%"></span></div></div><div class="result-source">Morgan (r=2, 2048 bit) · Tanimoto</div>`;
  } else {
    body = `<div class="calculation-title"><strong>${escapeHtml(call.name)}</strong></div>`;
  }
  $("result-panel").innerHTML = `<div class="calculation-card">${body}<details class="raw-result"><summary>参数与原始输出</summary><div class="result-source" data-no-i18n>${escapeHtml(result.source || "")}</div>${call.name === "molecular_similarity" ? "<p>结构相似度不代表生物活性。</p>" : ""}<pre>${escapeHtml(JSON.stringify({ arguments: call.arguments, result }, null, 2))}</pre></details></div>`;
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
  const revision = state.revision;
  sendChat($("message-input").value,revision ? revision.planRevision
    ? `修改方案/步骤：${revision.title}；先读取 calculation_plan_get，再用 calculation_plan_save 修改 plan_id=${revision.id}，expected_revision=${revision.planRevision}。`
    : "修改步骤："+revision.title+"；用 calculation_prepare.revises_preview_id 替换草案 "+revision.id : "");
});
$("message-input").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
    event.preventDefault();
    $("chat-form").requestSubmit();
  }
});
$("message-input").addEventListener("input", () => {
  saveDraft();
  $("message-input").style.height = "auto";
  $("message-input").style.height = `${Math.min($("message-input").scrollHeight, 180)}px`;
});
$("new-chat").addEventListener("click", newConversation);
$("session-list").addEventListener("click", (event) => {
  const button = event.target.closest("[data-session]");
  if (button) selectSession(button.dataset.session);
});
$("new-chat-list").addEventListener("click", newConversation);
$("load-more").addEventListener("click", async () => {
  $("load-more").disabled = true;
  try { await refreshSessions(true); } catch (error) { toast(error.message); }
  finally { $("load-more").disabled = false; }
});
$("conversation").addEventListener("click", async (event) => {
  if (event.target.closest("[data-task-panel]")) selectInspectorTab("calculation");
  const retry = event.target.closest("[data-retry-request]");
  if (retry && !state.busy) {
    const item = state.outbox.find(item=>item.id===retry.dataset.retryRequest);
    if (item) sendChat(item.body.message,item.body.context || "",[],{...item.body,request_id:item.id});
  }
  if (event.target.closest("#retry-message") && state.pending && !state.busy) {
    sendChat(state.pending.message, state.pending.context, state.pending.files);
  }
});
$("properties-form").addEventListener("submit", (event) => { event.preventDefault(); runLocalTool("molecular_properties", event.currentTarget); });
$("similarity-form").addEventListener("submit", (event) => { event.preventDefault(); runLocalTool("molecular_similarity", event.currentTarget); });
function setSidebarCollapsed(collapsed, reveal = false, persist = true) {
  document.body.classList.toggle("sidebar-collapsed", collapsed);
  $("sidebar-collapse").setAttribute("aria-expanded", String(!collapsed));
  $("sidebar-collapse").setAttribute("aria-label", collapsed ? "展开侧栏" : "折叠侧栏");
  $("sidebar-collapse").title = collapsed ? "展开侧栏" : "折叠侧栏";
  try { if (persist) localStorage.setItem("chemistry.sidebarCollapsed", String(collapsed)); } catch { /* Optional. */ }
  if (reveal && innerWidth <= 767) setDrawer("sidebar", !collapsed);
  syncDrawerAccessibility();
}
try {
  const saved = localStorage.getItem("chemistry.sidebarCollapsed");
  setSidebarCollapsed(saved === null ? innerWidth >= 768 && innerWidth < 1024 : saved === "true", false, false);
} catch { /* Use the desktop navigation by default. */ }
$("sidebar-collapse").addEventListener("click", () => setSidebarCollapsed(!document.body.classList.contains("sidebar-collapsed"), true));
$("menu-toggle").addEventListener("click", () => { setInspectorOpen(false, {persist:false, focus:false}); setDrawer("sidebar", !document.body.classList.contains("sidebar-open")); });
$("tools-toggle").addEventListener("click", () => { setDrawer("sidebar", false); setInspectorOpen(true); });
$("inspector-close").addEventListener("click", () => setDrawer("inspector", false));
$("screen-overlay").addEventListener("click", closeDrawers);
document.addEventListener("keydown", (event) => {
  if (document.querySelector("dialog[open]")) return;
  if (event.defaultPrevented) return;
  if (event.key === "Escape") {
    if (document.body.classList.contains("sidebar-open")) setDrawer("sidebar", false);
    else if (document.body.classList.contains("inspector-open")) setInspectorOpen(false);
  }
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
  if (innerWidth > 767) closeDrawers();
  syncDrawerAccessibility();
});

function showLogin(message = "") {
  $("workspace-settings").close();
  chatPositions.clear();progressFeed.reset();
  clearRevision(); viewer.reset(); calculations.reset(); notifications.enabled(false);
  resetContextMeter();
  state.accountVersion += 1;
  state.navigation += 1;
  rememberSession(null);
  inspectorSession = null;
  inspectorTabs.restore();
  setInspectorOpen(false, {focus:false, persist:false});
  state.sessionId = null;
  state.sessions = [];
  state.turns = [];
  state.runs = [];
  state.pending = null; state.outbox = []; state.chatSignature = "";
  state.auth.user = null;
  chatModels.reset();
  projectNavigation.reset();

  state.busy = false;
  state.loading = false;
  $("conversation").replaceChildren();
  $("session-list").replaceChildren();
  $("conversation-title").textContent = t("新对话");
  $("message-input").value = "";
  uploads.reset();
  titledSessions.clear();
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
  restoreLinkedSettings();
  const entry = new URL(location.href);
  const setup = entry.searchParams.get("setup") === "compute";
  let id = entry.searchParams.get("session");
  const storageKey = state.auth.user ? `chemistry.session.${state.auth.user.id}` : "chemistry.session";
  try { if (!setup) id ||= localStorage.getItem(storageKey); } catch { /* Optional persistence. */ }
  void refreshSessions().catch(error => toast(error.message));
  if (id) await selectSession(id, true);
  else { chatModels.newConversation(); void refreshHealth(); uploads.select(null); restoreInspector(null); $("conversation-title").textContent = t("新对话"); restoreDraft(); renderConversation(false); updateControls(); }
  if (setup) {
    const field = $("message-input");
    if (!field.value.trim() && !state.pending) field.value = t("请检查我的计算配置，告诉我还需要提供哪些服务器、软件、赝势和队列信息。");
    else toast(t("当前草稿已保留，可以在对话中询问计算配置。"));
    const url = new URL(location.href);
    url.searchParams.delete("setup"); history.replaceState(null,"",url);
    field.focus();
  }
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
setInspectorOpen(false, {focus:false, persist:false});
const restoreLinkedSettings = initSettingsDialog({
  onOpen() { if (document.body.classList.contains("sidebar-open")) setDrawer("sidebar", false); },
  async onClose() {
    if (!$("login-screen").hidden) return;
    await refreshHealth();
    if (state.health?.model_configured) void chatModels.refresh();
  },
});
initialize();
setInterval(() => { if ($("login-screen").hidden) refreshHealth(); }, 30000);

function setMeter(percent = null) {
  const ring = $("context-ring");
  ring.style.setProperty("--used", String(Math.min(100, Math.max(0, percent || 0))));
  ring.toggleAttribute("data-used", percent > 0);
  const label = t("上下文用量") + (percent === null ? "" : ` · ${percent}%`);
  ring.title = label;
  ring.setAttribute("aria-label", label);
}

function resetContextMeter(message = "暂无对话记录。") {
  contextGeneration++;
  nativeContext = null;
  contextSignature = "";
  contextRequest = null;
  $("context-ring").hidden = Boolean(state.health?.agent_backend && state.health.agent_backend !== "builtin");
  $("native-context-details").replaceChildren();
  $("native-context-details").hidden = true;
  $("context-budget").hidden = false;
  $("context-summary").hidden = false;
  setMeter();
  $("context-panel").close();
  $("context-status").textContent = t("上下文用量");
  $("context-budget").textContent = t(message);
  $("context-summary").textContent = "";
}

async function refreshContext() {
  const id = state.sessionId;
  if (!id) { resetContextMeter(); return; }
  const generation = ++contextGeneration;
  try {
    const info = await api(`/sessions/${encodeURIComponent(id)}/context`);
    if (id !== state.sessionId || generation !== contextGeneration) return;
    if (info.managed_by) {
      const signature = JSON.stringify(info) + document.documentElement.lang;
      if (signature === contextSignature) return;
      contextSignature = signature;
      nativeContext = info;
      const view = externalContextView(info, document.documentElement.lang || 'zh-CN');
      setMeter(view.percent);
      $("context-ring").hidden = !view.visible;
      $("context-status").textContent = view.title;
      $("context-budget").hidden = true;
      $("context-summary").hidden = true;
      const details = $("native-context-details");
      details.replaceChildren();
      for (const [label, value] of view.rows) {
        const term = document.createElement('dt'), description = document.createElement('dd');
        term.textContent = t(label); description.textContent = t(value);
        details.append(term, description);
      }
      details.hidden = !view.rows.length;
      updateControls();
      // One read-only native resume for old sessions; polling below only reads platform state.
      const syncKey = `${id}:${info.managed_by}`;
      if (info.has_thread && !info.updated_at && !info.busy && (!info.operation || (!info.updated_at && info.operation.state !== "running")) && !nativeContextSynced.has(syncKey)) {
        nativeContextSynced.add(syncKey);
        void api(`/sessions/${encodeURIComponent(id)}/context/refresh?operation_id=${crypto.randomUUID()}`, {method:'POST'}).catch(() => {});
      }
      return;
    }
    nativeContext = null;
    $("context-ring").hidden = false;
    $("native-context-details").hidden = true;
    $("context-budget").hidden = false;
    $("context-summary").hidden = false;
    updateControls();
    const percent = Math.round(100 * info.estimated_input_tokens / Math.max(1, info.input_budget_tokens));
    setMeter(percent);
    $("context-status").textContent = `会话记忆 · 已摘要 ${info.covered_messages} / ${info.original_messages} 条消息`;
    $("context-budget").textContent = `≈ ${info.estimated_input_tokens.toLocaleString()} / ${info.input_budget_tokens.toLocaleString()} tokens (${percent}%)`;
    $("context-summary").textContent = info.summary || "";
    $("context-summary").hidden = !info.summary;
  } catch (error) {
    if (id !== state.sessionId || generation !== contextGeneration) return;
    setMeter();
    $("context-status").textContent = t("上下文用量");
    $("context-budget").textContent = error.message;
    $("context-summary").textContent = "";
  }
}
$("compact-context").onclick = async () => {
  if (!state.sessionId || $("compact-context").disabled) return;
  const id = state.sessionId, external = Boolean(nativeContext);
  contextRequest ||= {session: id, id: crypto.randomUUID()};
  if (contextRequest.session !== id) contextRequest = {session: id, id: crypto.randomUUID()};
  state.busy = true; updateControls();
  try {
    const result = await api(`/sessions/${encodeURIComponent(id)}/context/compact?operation_id=${contextRequest.id}`, { method: "POST" });
    contextRequest = null;
    if (state.sessionId === id) await refreshContext();
    toast(external ? "已交给 Agent 压缩。" : result.context.summary_calls ? "历史已压缩" : "当前无需压缩");
  } catch (error) { toast(error.message); }
  finally { state.busy = false; updateControls(); }
};
setInterval(async () => {
  if (document.hidden || !state.sessionId || state.loading || contextPolling) return;
  if (state.health?.agent_backend === 'builtin') return;
  contextPolling = true;
  try { await refreshContext(); } finally { contextPolling = false; }
}, 4000);
