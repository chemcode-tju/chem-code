import { escapeHtml } from "./core.mjs";
import { structureButton, isStructureName } from "./structure-links.mjs";
import { t } from "./i18n.mjs";

export function createFileUploads({ state, toast, updateControls, onChange, onUnauthorized }) {
  const $ = (id) => document.getElementById(id);
  const drafts=new Map();
  let active;
  function select(session) {
    const key='chemistry.files.'+(state.auth.user?.id || 'local')+'.'+(session || 'new');
    if (!drafts.has(key)) {
      let files=[];
      try {files=JSON.parse(sessionStorage.getItem(key) || '[]');} catch {}
      drafts.set(key,{key,files:Array.isArray(files)?files:[],loading:false,controller:null});
    }
    active=drafts.get(key);closeMenus();
  }
  function persist(draft) {
    try {
      if(draft.files.length) sessionStorage.setItem(draft.key,JSON.stringify(draft.files));
      else sessionStorage.removeItem(draft.key);
    } catch {}
  }
  const menu = $("composer-upload-menu"), trigger = $("composer-add");
  function closeMenus() {
    menu.hidden = true;
    trigger.setAttribute("aria-expanded", "false");
  }
  trigger.addEventListener("click", () => {
    menu.hidden = !menu.hidden;
    trigger.setAttribute("aria-expanded", String(!menu.hidden));
    if (!menu.hidden) menu.querySelector("button").focus();
  });
  document.addEventListener("click", event => { if (!event.target.closest(".add-menu")) closeMenus(); });
  document.addEventListener("keydown", event => {
    if (event.key === "Escape" && !menu.hidden) { closeMenus(); trigger.focus(); }
  });
  document.querySelectorAll("[data-upload]").forEach((button) => button.addEventListener("click", () => {
    closeMenus(); $("file-picker").click();
  }));
  function render() {
    const files=active?.files || [], uploading=active?.loading;
    $("attachment-list").hidden = !files.length && !uploading;
    $("attachment-list").innerHTML = files.map((file) => `<div class="attachment-chip"><a href="/api/files/${file.id}" download data-no-i18n>${escapeHtml(file.name)}</a>${fileViewer(file)}<small>${t(file.text_available ? "文本可读取" : "已上传，未解析")}</small><button type="button" data-remove-file="${file.id}" aria-label="${escapeHtml(t("移除附件"))}"${state.busy ? " disabled" : ""}><svg class="icon" aria-hidden="true"><use href="/assets/ui-icons.svg#i-close"/></svg></button></div>`).join("") + (uploading ? `<span class="upload-progress">${t("正在上传…")}</span>` : "");
    updateControls(); onChange?.();
  }
  $("attachment-list").addEventListener("click", (event) => {
    const button = event.target.closest("[data-remove-file]");
    if (button && !state.busy) { active.files=active.files.filter(file=>file.id!==button.dataset.removeFile);persist(active);render(); }
  });
  $("file-picker").addEventListener("change", async () => {
    const selected = [...$("file-picker").files];
    $("file-picker").value = "";
    if (!active) select(state.sessionId);
    const draft=active, version=state.accountVersion;
    if (!selected.length || draft.loading || state.busy || state.loading) return;
    draft.controller=new AbortController();draft.loading=true;render();
    try {
      for (const file of selected) {
        if (version!==state.accountVersion || draft.controller.signal.aborted) break;
        try {
          const response=await fetch("/api/files",{method:"POST",headers:{
            "Content-Type":"application/octet-stream","X-File-Name":encodeURIComponent(file.name),
          },body:file,signal:draft.controller.signal});
          const data=await response.json().catch(()=>null);
          if(version!==state.accountVersion || draft.controller.signal.aborted) break;
          if(response.status===401) {onUnauthorized();break;}
          if(!response.ok) throw new Error(data?.error?.message || t("文件上传失败，请重试。"));
          draft.files.push(data);persist(draft);
        } catch(error) {
          if(version===state.accountVersion && !draft.controller.signal.aborted) toast(file.name+' · '+error.message);
        }
        if(draft===active) render();
      }
    } finally {
      draft.loading=false;draft.controller=null;
      if(draft===active) render();
    }
  });
  window.addEventListener("languagechange", render);
  return {
    get files() { return [...(active?.files || [])]; }, get uploading() { return Boolean(active?.loading); },
    select(session) { select(session);render(); },
    clear() { if(active) {active.files=[];persist(active);} closeMenus();render(); },
    reset() { for(const draft of drafts.values()) draft.controller?.abort();drafts.clear();active=null;closeMenus();render(); },
    render,
  };
}

function fileViewer(file) { return isStructureName(file.name) ? structureButton("file",file.id,file.name,"查看结构") : ""; }

export function renderFiles(files) {
  if (!files?.length) return "";
  return `<div class="message-files">${files.map((file) => `<span class="file-receipt"><a href="/api/files/${encodeURIComponent(file.id)}" download data-no-i18n>${escapeHtml(file.name)}</a>${fileViewer(file)}</span>`).join("")}</div>`;
}

export function contextFiles(context) {
  try { const data = JSON.parse(String(context).split(/\n\n(?=\{"uploaded_files":)/).at(-1)); return Array.isArray(data.uploaded_files) ? data.uploaded_files : []; }
  catch { return []; }
}
