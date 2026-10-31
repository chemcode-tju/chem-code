import { escapeHtml } from "./core.mjs";
import { t } from "./i18n.mjs";

export function createFileUploads({ state, toast, updateControls, onUnauthorized }) {
  const $ = (id) => document.getElementById(id);
  let files = [];
  let generation = 0;
  let uploading = false;
  const menus = [$("sidebar-upload-menu"), $("composer-upload-menu")];
  function closeMenus() {
    menus.forEach((menu) => { menu.hidden = true; });
    [$("sidebar-add"), $("composer-add")].forEach((button) => button.setAttribute("aria-expanded", "false"));
  }
  for (const prefix of ["sidebar", "composer"]) {
    $(`${prefix}-add`).addEventListener("click", () => {
      const open = $(`${prefix}-upload-menu`).hidden;
      closeMenus(); $(`${prefix}-upload-menu`).hidden = !open;
      $(`${prefix}-add`).setAttribute("aria-expanded", String(open));
      if (open) $(`${prefix}-upload-menu`).querySelector("button").focus();
    });
  }
  document.addEventListener("click", (event) => { if (!event.target.closest(".add-menu")) closeMenus(); });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    const open = menus.find((menu) => !menu.hidden);
    if (open) open.parentElement.querySelector("button").focus();
    closeMenus();
  });
  document.querySelectorAll("[data-upload]").forEach((button) => button.addEventListener("click", () => {
    closeMenus(); $("file-picker").click();
  }));
  function render() {
    $("attachment-list").hidden = !files.length && !uploading;
    $("attachment-list").innerHTML = files.map((file) => `<div class="attachment-chip"><a href="/api/files/${file.id}" download data-no-i18n>${escapeHtml(file.name)}</a><small>${t(file.text_available ? "文本可读取" : "已上传，未解析")}</small><button type="button" data-remove-file="${file.id}" aria-label="${escapeHtml(t("移除附件"))}"${state.busy ? " disabled" : ""}>×</button></div>`).join("") + (uploading ? `<span class="upload-progress">${t("正在上传…")}</span>` : "");
    updateControls();
  }
  $("attachment-list").addEventListener("click", (event) => {
    const button = event.target.closest("[data-remove-file]");
    if (button && !state.busy) { files = files.filter((file) => file.id !== button.dataset.removeFile); render(); }
  });
  $("file-picker").addEventListener("change", async () => {
    const selected = [...$("file-picker").files];
    $("file-picker").value = "";
    if (!selected.length || uploading || state.busy || state.loading) return;
    if (files.length + selected.length > 10) { toast(t("每条消息最多附加 10 个文件。")); return; }
    const token = generation;
    uploading = true; render();
    try {
      for (const file of selected) {
        if (file.size > 25 * 1024 * 1024) throw new Error(t("单个文件不能超过 25 MB。"));
        const response = await fetch("/api/files", { method: "POST", headers: {
          "Content-Type": "application/octet-stream", "X-File-Name": encodeURIComponent(file.name),
        }, body: file });
        const data = await response.json().catch(() => null);
        if (token !== generation) return;
        if (response.status === 401) { onUnauthorized(); return; }
        if (!response.ok) throw new Error(data?.error?.message || t("文件上传失败，请重试。"));
        files.push(data); render();
      }
    } catch (error) { if (token === generation) toast(error.message || t("文件上传失败，请重试。")); }
    finally { if (token === generation) { uploading = false; render(); } }
  });
  window.addEventListener("languagechange", render);
  return {
    get files() { return [...files]; }, get uploading() { return uploading; },
    clear() { generation++; files = []; uploading = false; closeMenus(); render(); },
    render,
  };
}

export function renderFiles(files) {
  if (!files?.length) return "";
  return `<div class="message-files">${files.map((file) => `<a href="/api/files/${encodeURIComponent(file.id)}" download data-no-i18n>↳ ${escapeHtml(file.name)}</a>`).join("")}</div>`;
}

export function contextFiles(context) {
  try { const data = JSON.parse(context); return Array.isArray(data.uploaded_files) ? data.uploaded_files : []; }
  catch { return []; }
}
