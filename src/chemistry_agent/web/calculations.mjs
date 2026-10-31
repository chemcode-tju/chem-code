import { escapeHtml as esc } from "./core.mjs";

export function createCalculations({ api, getSession, ensureSession, isBusy, toast, onConfirmed }) {
  const panel = document.getElementById("calculation-cards");
  const attachments = document.getElementById("structure-attachments");
  const input = document.getElementById("structure-file");
  const button = document.getElementById("structure-upload");
  let enabled = false, session = null, selected = [], records = null, pending = false;
  const path = (id) => `/api/automation/chat/${encodeURIComponent(id)}`;

  function drawAttachments() {
    attachments.innerHTML = selected.map((item) => `<span class="structure-chip">${esc(item.name)} · ${esc(item.formula)} · ${item.atoms} 原子 <button type="button" data-remove-structure="${esc(item.id)}" aria-label="移除结构附件">×</button></span>`).join("");
  }

  function render() {
    if (!enabled || !records) { panel.hidden = true; return; }
    const downloads = records.structures.map((item) => `<div class="structure-item"><span>${esc(item.name)} · ${esc(item.formula)} · ${item.atoms} 原子</span><button type="button" data-attach-structure="${esc(item.id)}">用于对话</button><a href="${path(session)}/files/structure/${esc(item.id)}">下载结构</a></div>`).join("");
    const models = records.models.map((item) => `<details class="model-record"><summary>建模代码 · ${esc(item.name)} · ${item.status === "completed" ? "已生成结构" : item.status === "failed" ? "执行失败" : "待核对"}</summary><pre>${esc(item.code)}</pre>${item.error ? `<p class="tool-failure">${esc(item.error)}</p>` : ""}<a href="${path(session)}/files/model/${esc(item.id)}">下载 model.py</a></details>`).join("");
    const previews = records.previews.map((item) => {
      const submission = records.submissions[item.preview_id];
      const job = submission?.job;
      const resource = item.resources;
      return `<article class="calculation-preview"><div class="calculation-card-heading"><strong>${esc(item.structure.formula)} · 计算预览</strong><span>${job ? `AiiDA #${job.pk}` : "等待确认"}</span></div>
        <p>${esc(item.server.label)} · ${esc(resource.queue)} · ${resource.nodes} 节点 × ${resource.ranks_per_node} MPI · ${resource.gpus_per_node || 0} ${esc(resource.gpu_type || "GPU")} / 节点 · ${resource.wallclock_seconds} 秒 · ${resource.memory_mb} MB</p>
        <p class="result-source">预览 ${esc(item.preview_id)}<br>输入摘要 ${esc(item.input_sha256)}</p>
        <details><summary>查看实际参数、结构和提交脚本</summary>${Object.entries(item.files).map(([name, content]) => `<h4>${esc(name)}</h4><pre>${esc(content)}</pre>`).join("")}</details>
        ${job ? `<p>已交给 AiiDA；提交时状态：${esc(job.process_state)}。最新状态请刷新或在对话中查询。</p><button type="button" data-refresh-job="${esc(job.id)}">查询最新状态与结果</button>` : `<button type="button" class="tool-submit" data-confirm-preview="${esc(item.preview_id)}">确认以上参数并提交计算</button>`}
        <div class="job-live-result" data-job-result="${esc(item.preview_id)}"></div></article>`;
    }).join("");
    panel.innerHTML = `<div class="section-heading"><h2>本次对话的结构与计算</h2><button type="button" id="refresh-calculations">刷新</button></div>${downloads}${models}${previews}`;
    panel.hidden = !(downloads || models || previews);
  }

  async function refresh() {
    const id = getSession();
    if (!enabled || !id) { records = null; session = id; render(); return; }
    if (session !== id) { selected = []; drawAttachments(); }
    session = id;
    const response = await api(`${path(id)}/artifacts`);
    if (id !== getSession()) return;
    records = response;
    render();
  }

  button.addEventListener("click", () => { if (!isBusy() && !pending) input.click(); });
  input.addEventListener("change", async () => {
    const file = input.files[0];
    if (!file || isBusy() || pending) return;
    pending = true; button.disabled = true;
    try {
      if (file.size > 2_000_000) throw new Error("结构文件不能超过 2 MB。");
      const id = await ensureSession();
      const format = /\.cif$/i.test(file.name) ? "cif" : /\.(extxyz|xyz)$/i.test(file.name) ? "extxyz" : "vasp";
      const result = await api(`${path(id)}/structures`, { method: "POST", body: JSON.stringify({ name: file.name.slice(0, 120), structure: { format, content: await file.text() } }) });
      if (id !== getSession()) return;
      await refresh();
      selected = [...selected.filter((item) => item.id !== result.id), result].slice(-8);
      drawAttachments(); toast("结构已验证并附加；发送消息即可让 Agent 使用。");
    } catch (error) { toast(error.message); }
    finally { pending = false; input.value = ""; button.disabled = false; }
  });
  attachments.addEventListener("click", (event) => {
    const target = event.target.closest("[data-remove-structure]");
    if (target && !isBusy()) { selected = selected.filter((item) => item.id !== target.dataset.removeStructure); drawAttachments(); }
  });
  panel.addEventListener("click", async (event) => {
    const target = event.target.closest("button");
    if (!target || isBusy() || pending) return;
    if (target.dataset.attachStructure) {
      const item = records.structures.find((value) => value.id === target.dataset.attachStructure);
      selected = [...selected.filter((value) => value.id !== item.id), item].slice(-8);
      drawAttachments(); return;
    }
    pending = true; target.disabled = true;
    const id = getSession();
    try {
      if (target.dataset.confirmPreview) {
        await api(`${path(id)}/confirm/${encodeURIComponent(target.dataset.confirmPreview)}`, { method: "POST" });
        if (id === getSession()) await onConfirmed(id);
      } else if (target.dataset.refreshJob) {
        const result = await api(`/api/automation/jobs/${encodeURIComponent(target.dataset.refreshJob)}`);
        if (id === getSession()) target.parentElement.querySelector(".job-live-result").textContent = JSON.stringify(result, null, 2);
      } else if (target.id === "refresh-calculations") await refresh();
    } catch (error) { toast(error.message); }
    finally { pending = false; target.disabled = false; }
  });

  return {
    refresh,
    enabled(value) { enabled = value; button.hidden = !enabled; if (!enabled) { selected = []; records = null; drawAttachments(); render(); } },
    selectedIds() { return selected.map((item) => item.id); },
    clearSelection() { selected = []; drawAttachments(); },
    reset() { selected = []; records = null; session = null; drawAttachments(); render(); },
    busy() { return pending; },
  };
}
