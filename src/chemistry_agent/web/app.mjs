import { externalContextView } from "./external-context.mjs";
import {createWorkspaceFiles} from "./workspace-files.mjs";
import {createFileBrowser} from "./file-browser.mjs";
import {userQuestions} from './user-questions.mjs';
import {visibleReference, activity, activityOpen, activitySteps, answerBox, answerFooter, liveAnswerFooter, initAnswerControls, initDeferredActivities} from "./answer-ui.mjs";
import {liveAnswer} from "./live-answer.mjs";
import {addressDraft, groupMessages} from "./group-messages.mjs";
import {createProgressFeed} from './progress-feed.mjs';
import { settingsHref, initSettingsDialog } from "./settings-dialog.mjs";
import {initWorkspaceControls} from './workspace-controls.mjs';
import {resultFigures} from './result-figures.mjs';
import { initInspectorTabs } from "./inspector-tabs.mjs";
import { createStructureBridge, createStructureDocuments, structureButton, inlineStructureName, isStructureName, getFileBlob } from "./structure-links.mjs";
import { createCalculations } from "./calculations.mjs?v=20261102-general-plans";
import {initJobMonitor} from "./job-monitor.mjs";
import {initJobBoard} from "./job-board.mjs";
import {createMemoryWorkspace} from "./memory.mjs";
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
const splitWorkspace = matchMedia("(min-width:688px)");
const dockedNavigation = matchMedia("(min-width:1024px)");
const setDockMaximized = initWorkspaceControls({closeInspector:()=>setInspectorOpen(false), onMaximized:syncDrawerAccessibility});
const jobMonitor = initJobMonitor();
const jobBoard = initJobBoard();
const workspaces = {
  chat: {path:"/", entry:"chat-workspace-entry", panel:"chat-panel", heading:"conversation-title"},
  monitor: {path:"/monitor", entry:"job-monitor-entry", panel:"queue-page", heading:"queue-heading"},
  board: {path:"/monitor/board", entry:"job-board-entry", panel:"job-board-page", heading:"job-board-heading"},
  memory: {path:"/memory", entry:"memory-entry", panel:"memory-page", heading:"memory-heading"},
};
let activeWorkspace = "chat";
let restoreChatInspector = false;
let sidebarDrawerOpen = false;
let preferredSidebarCollapsed = false;
try { preferredSidebarCollapsed = localStorage.getItem("chemistry.sidebarCollapsed") === "true"; } catch { /* Optional. */ }
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
const notifications = createNotifications({api, selectSession:openWorkspaceSession});
const memoryWorkspace = createMemoryWorkspace({api, markdown, updateLocation:rememberMemoryLocation, openSession:openWorkspaceSession});
const inspectorTabs = initInspectorTabs(open => {
  if (open) setSidebarDrawer(false);
  setInspectorOpen(open, {focus:false});
});
const selectInspectorTab = inspectorTabs.select;
const viewer = createStructureBridge({selectTab:selectInspectorTab, toast});
const structureDocuments = createStructureDocuments({inspector:inspectorTabs, toast});
const fileBrowser = createFileBrowser({api, session:()=>state.sessionId, viewer:structureDocuments});
document.addEventListener('click', event => {
  const action = event.target.closest('[data-preview-attachment]');
  if (!action) return;
  selectInspectorTab('file');
  void fileBrowser.openAttachment(action.dataset.previewAttachment, action.dataset.previewName);
});
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
    if (!splitWorkspace.matches) setInspectorOpen(false);
    state.revision = {id,title,planRevision};
    $("revision-chip").hidden=false;
    $("revision-chip").textContent="修改："+title;
    $("revision-chip").insertAdjacentHTML("beforeend", icon("close"));
    field.focus();
  },
  viewer, openAttachment:fileBrowser.openAttachmentStructure,
  chatFiles: () => [...state.turns.flatMap(turn => turn.files?.length ? turn.files : contextFiles(turn.context)), ...uploads.files],
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
  $("model-status").href = settingsHref("agent", id);
  document.querySelector(".workspace-rail-settings").href = settingsHref("general", id);
  for (const [name, {path, entry}] of Object.entries(workspaces)) {
    if (name === "memory" && activeWorkspace === "memory") continue;
    $(entry).href = workspaceHref(path, id);
  }
  const storageKey = state.auth.user ? `chemistry.session.${state.auth.user.id}` : "chemistry.session";
  try {
    if (id) localStorage.setItem(storageKey, id);
    else localStorage.removeItem(storageKey);
  } catch { /* Storage can be unavailable in a private browser. */ }
  if (history.state?.workspaceSettings || location.pathname === "/memory") return;
  const url = new URL(location.href);
  if (id) url.searchParams.set("session", id);
  else url.searchParams.delete("session");
  history.replaceState(null, "", url);
}

function workspaceHref(path, id) {
  return id ? `${path}?session=${encodeURIComponent(id)}` : path;
}

function rememberMemoryLocation(params) {
  if (activeWorkspace !== "memory" || location.pathname !== "/memory") return;
  const href = "/memory" + (params.size ? "?" + params : "");
  $("memory-entry").href = href;
  history.replaceState(null, "", href);
}

function syncDrawerAccessibility() {
  const loginRequired = !$("login-screen").hidden;
  const inspectorHidden = loginRequired || sidebarDrawerOpen || !document.body.classList.contains("inspector-open");
  const chat = $("chat-panel");
  chat.inert = loginRequired || sidebarDrawerOpen || document.body.classList.contains("dock-max");
  $("sidebar").inert = loginRequired;
  $("inspector").inert = inspectorHidden;
  $("inspector").setAttribute("aria-hidden", String(inspectorHidden));
  $("queue-page").inert = loginRequired;
  $("job-board-page").inert = loginRequired;
  $("memory-page").inert = loginRequired;
}

function setWorkspace(name, {navigate = false, focus = false, chatSession = state.sessionId} = {}) {
  if (name === activeWorkspace) {
    if (name === "memory" && !navigate) void memoryWorkspace.activate(location.search);
    return;
  }
  const workspace = workspaces[name];
  const entry = $(workspace.entry);
  const chat = name === "chat";
  if ((name === "monitor" || name === "board") && (state.auth.enabled || (entry.hidden && navigate))) return;

  if (!chat && activeWorkspace === "chat") {
    restoreChatInspector = document.body.classList.contains("inspector-open");
    setSidebarDrawer(false, {restoreFocus:false});
    setInspectorOpen(false, {focus:false,persist:false});
  }
  if (activeWorkspace === "memory") memoryWorkspace.closeDialogs();
  activeWorkspace = name;
  for (const [key, view] of Object.entries(workspaces)) {
    $(view.panel).hidden = name !== key;
    $(view.entry).removeAttribute("aria-current");
  }
  entry.setAttribute("aria-current", "page");
  if (navigate) history.pushState(null, "", chat ? workspaceHref(workspace.path, chatSession) : entry.href);

  syncSidebarNavigation();
  jobMonitor.show(name === "monitor");
  jobBoard.show(name === "board");
  if (chat && restoreChatInspector) setInspectorOpen(true, {focus:false,persist:false});
  if (name === "memory") void memoryWorkspace.activate(location.search);
  if (focus) $(workspace.heading).focus({preventScroll:true});
}

function workspaceForPath(path) {
  return Object.keys(workspaces).find(name => workspaces[name].path === path) || "chat";
}

async function openWorkspaceSession(session) {
  if (state.busy) return;
  setWorkspace("chat", {navigate:true,chatSession:session});
  if (session !== state.sessionId) await selectSession(session);
  $("conversation-title").focus({preventScroll:true});
}

for (const [name, {entry}] of Object.entries(workspaces)) {
  $(entry).addEventListener("click", event => {
    if (event.button || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    if (name === "chat" && activeWorkspace === "chat") {
      toggleChatSidebar({keepFocus:true});
      return;
    }
    setWorkspace(name, {navigate:true,focus:event.detail === 0});
  });
}
for (const brand of [$("rail-brand"), document.querySelector(".sidebar-header .brand")].filter(Boolean)) {
  brand.addEventListener("click", event => {
    if (event.button || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    if (activeWorkspace !== "chat") setWorkspace("chat", {navigate:true, focus:event.detail === 0});
    else if (sidebarDrawerOpen) setSidebarDrawer(false);
  });
}
window.addEventListener("popstate", () => {
  if (history.state?.workspaceSettings || !$("login-screen").hidden) return;
  const workspace = workspaceForPath(location.pathname);
  setWorkspace(workspace);
  if (workspace !== "chat" || state.busy) return;
  const id = new URLSearchParams(location.search).get("session");
  if (id === state.sessionId) return;
  if (id) void selectSession(id, true);
  else newConversation({restoreSavedDraft:true});
});

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
  if (activeWorkspace !== "chat" && open) { restoreChatInspector = true; return; }
  const wasFocused = $("inspector").contains(document.activeElement);
  if (open) {
    inspectorTabs.loadActive();
    if (inspectorTabs.active() === "file") fileBrowser.activate();
  }
  document.body.classList.toggle("inspector-open", open);
  if (!open) setDockMaximized(false);
  const toolsToggle = $("tools-toggle");
  const toolsLabel = t(open ? "收起研究面板" : "展开研究面板");
  toolsToggle.title = toolsLabel;
  toolsToggle.setAttribute("aria-label", toolsLabel);
  $("tools-toggle").setAttribute("aria-expanded", String(open));
  $("inspector-close").setAttribute("aria-expanded", String(open));
  if (persist) saveInspector(inspectorSession);
  syncDrawerAccessibility();
  if (focus && open) inspectorTabs.focusActive();
  else if (focus && wasFocused) $("tools-toggle").focus({preventScroll:true});
}

function syncSidebarNavigation() {
  const chat = activeWorkspace === "chat";
  const docked = dockedNavigation.matches;
  const expanded = chat && (docked ? !preferredSidebarCollapsed : sidebarDrawerOpen);
  const collapseButton = $("sidebar-collapse");
  const expandButton = $("sidebar-expand");
  document.body.classList.toggle("sidebar-collapsed", !docked || !expanded);
  document.body.classList.toggle("sidebar-open", sidebarDrawerOpen);
  $("screen-overlay").hidden = !sidebarDrawerOpen;
  $("sidebar-content").hidden = !expanded;
  collapseButton.hidden = !expanded;
  collapseButton.setAttribute("aria-expanded", String(expanded));
  collapseButton.title = t("折叠侧栏");
  collapseButton.setAttribute("aria-label", collapseButton.title);
  expandButton.hidden = !chat || expanded;
  expandButton.setAttribute("aria-expanded", String(expanded));
  expandButton.title = t("展开侧栏");
  expandButton.setAttribute("aria-label", expandButton.title);
  syncDrawerAccessibility();
}

function setSidebarDrawer(open, {restoreFocus = true} = {}) {
  if (open === sidebarDrawerOpen) return;
  const focusedInside = $("sidebar-content").contains(document.activeElement);
  sidebarDrawerOpen = open;
  syncSidebarNavigation();
  if (!open && restoreFocus && focusedInside && activeWorkspace === "chat") {
    $("sidebar-expand").focus({preventScroll:true});
  }
}

function toggleChatSidebar({keyboard = false, keepFocus = false} = {}) {
  if (activeWorkspace !== "chat") return;
  if (!dockedNavigation.matches) {
    const opening = !sidebarDrawerOpen;
    setSidebarDrawer(opening, {restoreFocus: !keepFocus});
    if (opening && keyboard && !keepFocus) {
      ($("new-chat").disabled ? $("sidebar-collapse") : $("new-chat")).focus({preventScroll:true});
    }
    return;
  }
  const focusedInside = $("sidebar-content").contains(document.activeElement);
  preferredSidebarCollapsed = !preferredSidebarCollapsed;
  try { localStorage.setItem("chemistry.sidebarCollapsed", String(preferredSidebarCollapsed)); } catch { /* Optional. */ }
  syncSidebarNavigation();
  if (keepFocus) return;
  if (!preferredSidebarCollapsed && keyboard) {
    $("sidebar-collapse").focus({preventScroll:true});
  } else if (preferredSidebarCollapsed && (keyboard || focusedInside)) {
    $("sidebar-expand").focus({preventScroll:true});
  }
}

function closeDrawers() {
  setSidebarDrawer(false);
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
  for (const id of ['host-picker','folder-picker','folder-clear','monitor-picker']) $(id).disabled = Boolean(locked || state.metadataLoading);
  $("message-input").disabled = state.loading;
  updateMentions($("conversation"));
  $("send-button").disabled = locked || state.metadataLoading || uploads.uploading || !(state.health && (chatModels?.grouped || state.health.model_configured));
  $("composer-add").disabled = locked || uploads.uploading;
  const sendLabel = t(state.busy ? "正在生成回答" : "发送消息");
  $("send-button").title = sendLabel;
  $("send-button").setAttribute("aria-label", sendLabel);
  $("new-chat").disabled = locked;
  $("new-project").disabled = locked;
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
    $("job-monitor-entry").hidden = !state.health.queue_monitor_enabled;
    $("job-board-entry").hidden = !state.health.queue_monitor_enabled;
    calculations.enabled(Boolean(state.health.automation_enabled));
    notifications.enabled(Boolean(state.health.automation_enabled));
    chatModels?.render();
    const names = { "deepseek-flash": "DeepSeek Flash", "deepseek-v4-pro": "DeepSeek Pro" };
    $("model-label").textContent = state.health.model_configured
      ? `${names[state.health.model] || state.health.model} · ${state.health.provider === "chatgpt" ? "已登录" : "已配置"}` : "模型未配置";
    $("model-status").classList.toggle("unavailable", !chatModels?.grouped && !state.health.model_configured);
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
chatModels = createChatModelPicker({
  api, state, toast, updateControls, refreshHealth, onLocation:fileBrowser.syncProject,
  openSettingsTab:(tab, host)=>settingsDialog.open(tab, state.sessionId, host),
});
const uploads = createFileUploads({ state, toast, updateControls, onChange: () => calculations.drawStructures(), onUnauthorized: () => showLogin("登录已过期，请重新登录。") });

function renderSessions() {
  fileBrowser.syncSession();
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

const workspaceFiles=createWorkspaceFiles({api,session:()=>state.sessionId,
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

function updateMentions(root) {
  const roster = chatModels?.mentionable || [];
  for (const button of root.querySelectorAll('[data-mention-member]')) {
    const matches = roster.filter(member => member.name.toLowerCase() === button.dataset.memberName.toLowerCase());
    button.hidden = state.metadataLoading || matches.length !== 1 || matches[0].id !== button.dataset.mentionMember;
  }
}

$("conversation").addEventListener('click', event => {
  const button = event.target.closest('[data-mention-member]');
  if (!button || button.hidden) return;
  const input = $("message-input");
  input.value = addressDraft(input.value, button.dataset.memberName, chatModels.mentionable);
  input.dispatchEvent(new Event('input', {bubbles:true}));
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
});

function turnAnswer(turn,index) {
  const ask = turn.calls.find(call=>call.name==="ask_user_question" && call.success && call.result?.status==="awaiting_user");
  const speeches = turn.groupMessages ? groupMessages(turn.groupMessages, markdown) : '';
  if (!ask) return speeches || answerBox(markdown.render(turn.answer));
  const active=!state.turns.slice(index+1).some(other=>!other.event) && !state.outbox.length && !state.pending;
  return speeches + questionCard(ask.result,active);
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
      `<div class="assistant-message"><div class="assistant-body">${activity({id:item.run_id || item.id,start:item.started_at || (item.status==='running'?item.created_at:null),end:item.finished_at,calls:item.tools || [],steps:item.steps || [],running:item.status==='running',draftRequest:item.id})}<div data-live-group></div>${item.partial_group?.length ? groupMessages(item.partial_group, markdown) : item.partial_answer ? answerBox(markdown.render(item.partial_answer)) : ''}${label?`<div class="${failed?'message-error':'thinking-indicator'}">${escapeHtml(label)}</div>`:''}${item.status === "failed" ? `<button class="retry-button" data-retry-request="${escapeHtml(item.id)}">重试这条消息</button>` : ""}${liveAnswerFooter(item.id)}</div></div>`;
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
  if (id !== state.sessionId) { resetContextMeter("正在读取上下文…"); progressFeed.reset(id); clearRevision(); state.sessionId = null; state.turns = []; state.runs = []; state.outbox = []; state.chatSignature = ""; viewer.reset(); structureDocuments.reset(); calculations.reset(id); }
  workspaceFiles.reset();
  uploads.select(id);
  calculations.clearSelection();
  const navigation = ++state.navigation;
  state.sessionId = id;
  fileBrowser.syncSession();
  state.health = null;
  state.metadataLoading = true;
  state.loading = true;
  state.pending = null;
  renderConversation(false);
  setSidebarDrawer(false);
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
    viewer.reset(); structureDocuments.reset(); calculations.reset();
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

function newConversation({restoreSavedDraft = false} = {}) {
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
  viewer.reset(); structureDocuments.reset(); calculations.reset();
  resetContextMeter();
  rememberSession(null);
  restoreInspector(null, !restoreSavedDraft);
  $("conversation-title").textContent = t("新对话");
  $("message-input").value = "";
  if (restoreSavedDraft) restoreDraft();
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
  render(request, text, steps, group) {
    const el = document.querySelector(`[data-live-request="${CSS.escape(request)}"]`);
    if (!el) return;
    const scroll = $("chat-scroll");
    const pinned = scroll.scrollHeight - scroll.clientHeight - scroll.scrollTop < 56;
    const activity = el.closest('[data-activity]');
    if (steps && activity.dataset.steps !== JSON.stringify(steps)) {
      activity.dataset.steps = JSON.stringify(steps);
      activity.querySelector('[data-activity-steps]').innerHTML = activitySteps(steps);
    }
    if (group?.length) {
      const feed = activity.parentElement.querySelector('[data-live-group]');
      if (feed.messages !== group) {
        feed.messages = group;
        feed.groupPrefixLength = group.map(speech => '### ' + speech.name + '\n\n' + speech.answer).join('\n\n').length;
        feed.innerHTML = groupMessages(group, markdown);
        updateMentions(feed);
      }
      const current = text.slice(feed.groupPrefixLength);
      const body = feed.lastElementChild.querySelector('.markdown');
      if (body.dataset.draft !== current) {
        body.dataset.draft = current;
        body.innerHTML = markdown.renderPartial?.(current) ?? markdown.render(current);
      }
      el.dataset.preview = text;
    } else if (el.dataset.preview !== text) {
      el.dataset.preview = text;
      el.innerHTML = text ? (markdown.renderPartial?.(text) ?? markdown.render(text)) : "";
    }
    el.parentElement.hidden = !text || Boolean(group?.length);
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
  if (!(state.health && (chatModels.grouped || state.health.model_configured))) { toast("请先连接并配置模型。"); return; }
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
      await chatModels.saveNewSession(session.id, selectedModel);
      if (accountVersion !== state.accountVersion) return;
      state.sessionId = session.id;
      fileBrowser.syncSession();
      $("conversation-title").textContent = session.title;
      rememberSession(session.id);
      adoptInspector(session.id);
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
function syncWorkspaceLayout() {
  if (dockedNavigation.matches) setSidebarDrawer(false);
  syncSidebarNavigation();
  if (!splitWorkspace.matches) setDockMaximized(false);
}
syncWorkspaceLayout();
dockedNavigation.addEventListener("change", syncWorkspaceLayout);
splitWorkspace.addEventListener("change", syncWorkspaceLayout);

for (const button of [$("sidebar-expand"), $("sidebar-collapse")]) {
  button.addEventListener("click", event => toggleChatSidebar({keyboard: event.detail === 0}));
}
$("tools-toggle").addEventListener("click", () => {
  setSidebarDrawer(false);
  setInspectorOpen(!document.body.classList.contains("inspector-open"));
});
$("inspector-close").addEventListener("click", () => setInspectorOpen(false));
$("screen-overlay").addEventListener("click", closeDrawers);
document.addEventListener("keydown", (event) => {
  if (document.querySelector("dialog[open]")) return;
  if (event.defaultPrevented) return;
  if (event.key === "Escape") {
    if (document.body.classList.contains("sidebar-open")) setSidebarDrawer(false);
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

function showLogin(message = "") {
  $("workspace-settings").close();
  memoryWorkspace.reset();
  chatPositions.clear();progressFeed.reset();
  clearRevision(); viewer.reset(); structureDocuments.reset(); calculations.reset(); notifications.enabled(false);
  resetContextMeter();
  state.accountVersion += 1;
  state.navigation += 1;
  rememberSession(null);
  inspectorSession = null;
  inspectorTabs.restore();
  setInspectorOpen(false, {focus:false, persist:false});
  state.sessionId = null;
  state.sessions = [];
  fileBrowser.reset();
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
  const entry = new URL(location.href);
  const setup = entry.searchParams.get("setup") === "compute";
  let id = entry.searchParams.get("session");
  const storageKey = state.auth.user ? `chemistry.session.${state.auth.user.id}` : "chemistry.session";
  try { if (!setup) id ||= localStorage.getItem(storageKey); } catch { /* Optional persistence. */ }
  setWorkspace(workspaceForPath(entry.pathname));
  const opening = id ? selectSession(id, true) : null;
  if (!opening) {
    chatModels.newConversation();
    void refreshHealth();
    uploads.select(null);
    restoreInspector(null);
    $("conversation-title").textContent = t("新对话");
    restoreDraft();
    renderConversation(false);
    updateControls();
  }
  document.body.classList.remove("auth-checking");
  syncDrawerAccessibility();
  settingsDialog.restoreLinkedSettings();
  void refreshSessions().catch(error => toast(error.message));
  if (opening) await opening;
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
  syncSidebarNavigation();
});
setInspectorOpen(false, {focus:false, persist:false});
const settingsDialog = initSettingsDialog({
  onOpen() { if (document.body.classList.contains("sidebar-open")) setSidebarDrawer(false); },
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
  $("context-percent").hidden = percent === null;
  $("context-percent").textContent = percent === null ? "" : `${percent}%`;
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
  $("context-extra").hidden = true;
  $("context-extra").open = false;
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
      const usage = view.rows.find(([label])=>label === "上下文" || label === "上下文窗口");
      $("context-budget").hidden = false;
      $("context-budget").textContent = usage?.[1] || t("暂无用量数据");
      $("context-summary").hidden = true;
      const details = $("native-context-details");
      details.replaceChildren();
      for (const [label, value] of view.rows.filter(row => row !== usage)) {
        const term = document.createElement('dt'), description = document.createElement('dd');
        term.textContent = t(label); description.textContent = t(value);
        details.append(term, description);
      }
      details.hidden = !details.childElementCount;
      $("context-extra").hidden = details.hidden && !view.manual;
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
    const used = info.estimated_input_tokens, capacity = info.input_budget_tokens;
    const available = Number.isFinite(used) && Number.isFinite(capacity) && capacity > 0;
    const percent = available ? Math.round(100 * used / capacity) : null;
    setMeter(percent);
    $("context-status").textContent = t("上下文用量");
    $("context-extra").hidden = $("compact-context").hidden && !info.summary;
    $("context-budget").textContent = available ? `≈ ${used.toLocaleString()} / ${capacity.toLocaleString()} tokens` : t("暂无用量数据");
    $("context-summary").textContent = info.summary || "";
    $("context-summary").hidden = !info.summary;
  } catch (error) {
    if (id !== state.sessionId || generation !== contextGeneration) return;
    setMeter();
    $("context-status").textContent = t("上下文用量");
    $("context-extra").hidden = true;
    $("context-budget").hidden = false;
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
