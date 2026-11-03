import { t } from "./i18n.mjs";
import { escapeHtml } from "./core.mjs";

/** Folder navigation owns only organization; chat execution stays in app.mjs. */
export function createProjectNavigation({ api, state, selectSession, newConversation, refreshSessions, toast, sessionButton }) {
  const $ = (id) => document.getElementById(id);
  let projects = [];
  let selected = "";
  let editing = null;
  const expanded = new Set();
  let accountId = null;
  let revealedSession = null;
  const folderIcon = '<svg class="icon" aria-hidden="true"><use href="/assets/ui-icons.svg#i-folder"/></svg>';
  const chevron = '<svg class="icon disclosure" aria-hidden="true"><use href="/assets/ui-icons.svg#i-chevron"/></svg>';
  const currentProject = () => projects.find((p) => p.sessions.some((s) => s.id === state.sessionId))?.id || "";
  function render() {
    if (state.sessionId) selected = currentProject();
    if (state.sessionId !== revealedSession) {
      if (selected) expanded.add(selected);
      revealedSession = state.sessionId;
    }
    const picker = $("draft-project");
    picker.replaceChildren(new Option(t("个人工作空间"), ""), ...projects.map(project => {
      const option = new Option(project.name, project.id);
      option.dataset.noI18n = "";
      return option;
    }));
    picker.value = selected;
    const label = $("draft-project-name");
    label.toggleAttribute("data-no-i18n", Boolean(selected));
    label.textContent = picker.selectedOptions[0]?.textContent || t("个人工作空间");
    $("project-list").innerHTML = projects.map((project) => `<details class="project-folder" data-project="${project.id}"${expanded.has(project.id) ? " open" : ""}><summary>${folderIcon}<span title="${escapeHtml(project.name)}">${escapeHtml(project.name)}</span><small>${project.sessions.length}</small>${chevron}</summary><div class="project-children">${project.sessions.map(sessionButton).join("") || '<p class="sidebar-empty">暂无对话</p>'}<div class="project-actions"><button data-new-in="${project.id}"${state.busy || state.loading ? " disabled" : ""}><svg class="icon" aria-hidden="true"><use href="/assets/ui-icons.svg#i-plus"/></svg> 新对话</button><button data-edit-project="${project.id}">管理项目</button></div></div></details>`).join("") || '<p class="sidebar-empty">暂无项目</p>';

  }
  function reset() {
    projects = []; selected = ""; editing = null; expanded.clear(); accountId = null; revealedSession = null;
    $("project-dialog").close();
    render();
  }
  function accept(data) {
    const id = state.auth.user?.id || "local";
    if (accountId !== id) { expanded.clear(); selected = ""; accountId = id; }
    projects = data;
    if (selected && !projects.some((p) => p.id === selected)) selected = "";
  }
  function openEditor(id = null) {
    editing = id;
    $("project-dialog-title").textContent = id ? "管理项目" : "新建项目";
    $("project-name").value = projects.find((p) => p.id === id)?.name || "";
    $("delete-project").hidden = !id;
    $("project-hint").textContent = id ? "删除文件夹会保留所有对话，并将它们移回聊天列表。" : "将相关对话收纳在同一个项目中。";
    $("project-error").textContent = "";
    $("project-dialog").showModal();
    $("project-name").focus();
  }
  async function mutate(operation) {
    const version = state.accountVersion;
    const buttons = [...$("project-form").querySelectorAll("button")];
    buttons.forEach((b) => { b.disabled = true; });
    try {
      await operation();
      if (version !== state.accountVersion) return;
      await refreshSessions();
      $("project-dialog").close();
    } catch (error) {
      if (version === state.accountVersion) $("project-error").textContent = error.message;
    } finally { buttons.forEach((b) => { b.disabled = false; }); }
  }
  $("draft-project").addEventListener("change", (event) => {
    selected = event.target.value;
    if (selected) expanded.add(selected);
    const project = projects.find(item => item.id === selected);
    $("conversation-title").textContent = project ? `${project.name} / ${t("新对话")}` : t("新对话");
    render();
  });
  $("new-project").addEventListener("click", () => openEditor());
  $("cancel-project").addEventListener("click", () => $("project-dialog").close());
  $("project-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const name = $("project-name").value.trim();
    if (!name) { $("project-error").textContent = "请输入项目名称。"; return; }
    mutate(async () => {
      const project = await api(`/api/projects${editing ? `/${editing}` : ""}`, { method: editing ? "PATCH" : "POST", body: JSON.stringify({ name }) });
      expanded.add(project.id);
    });
  });
  $("delete-project").addEventListener("click", () => mutate(async () => {
    await api(`/api/projects/${editing}`, { method: "DELETE" });
    if (selected === editing) selected = "";
    toast("已删除文件夹，对话保留在聊天列表。");
  }));
  $("project-list").addEventListener("toggle", (event) => {
    if (!event.target.isConnected) return;
    const id = event.target.dataset.project;
    if (id) event.target.open ? expanded.add(id) : expanded.delete(id);
  }, true);
  $("project-list").addEventListener("click", (event) => {
    const session = event.target.closest("[data-session]");
    if (session) selectSession(session.dataset.session);
    const edit = event.target.closest("[data-edit-project]");
    if (edit) openEditor(edit.dataset.editProject);
    const create = event.target.closest("[data-new-in]");
    if (create && !state.busy && !state.loading) {
      newConversation(); selected = create.dataset.newIn; expanded.add(selected); render();
      $("conversation-title").textContent = `${projects.find((p) => p.id === selected)?.name} / ${t("新对话")}`;
    }
  });
  async function assign(sessionId) {
    await api(`/api/projects/sessions/${encodeURIComponent(sessionId)}`, { method: "PUT", body: JSON.stringify({ project_id: selected || null }) });
    if (selected) expanded.add(selected);
  }
  return { accept, render, reset, assign, clearSelection() { selected = ""; },
    includes(sessionId) { return projects.some((p) => p.sessions.some((s) => s.id === sessionId)); } };
}
