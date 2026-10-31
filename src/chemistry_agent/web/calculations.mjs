import { escapeHtml as esc } from "./core.mjs";

export function renderTaskStatus(job, recovery = null) {
  const recoveryLabels = {queued: "等待分析", running: "正在分析", diagnosed: "已诊断", needs_user: "需要你处理", submitting: "正在提交恢复任务", submitted: "已提交恢复任务", awaiting_confirmation: "修复预览待确认", interrupted: "上次分析中断，可重新唤起", disabled: "已关闭"};
  const energy = job.results?.total_energies?.energy_extrapolated;
  const status = job.results?.run_status || {};
  const title = job.display_title || (job.finished_ok ? "计算完成，结果已取回" : job.process_status || job.process_state);
  const errors = job.recent_process_errors || [];
  const steps = job.steps || [];
  const schedulerStale = job.scheduler_job_id && !['finished', 'excepted', 'killed'].includes(job.process_state) &&
    (!job.scheduler_checked_at || Date.now() - Date.parse(job.scheduler_checked_at) > 600000);
  return `<strong>${esc(title || "正在同步")}${job.paused ? " · 已暂停" : ""}</strong>
    <p>AiiDA #${job.pk} · ${job.scheduler_job_id ? `调度器 ${esc(job.scheduler_job_id)}` : job.workflow ? "多步骤流程" : "尚未进入调度器队列"}</p>
    ${typeof energy === "number" && Number.isFinite(energy) ? `<p>外推总能量：${energy} eV</p>` : ""}
    ${job.finished_ok && job.results ? `<p>电子收敛：${status.electronic_converged === true ? "通过" : "待核验"}${status.nsw > 0 ? ` · 结构收敛：${status.ionic_converged === true ? "通过" : "未通过"}` : ""} · 科学精度尚未验证</p>` : ""}
    ${!job.finished_ok && errors.length ? `<p>${esc(errors[0].message)}</p>` : ""}
    ${schedulerStale ? '<p>调度器状态待同步，当前显示最后已知状态。</p>' : ''}
    ${steps.map((step) => `<p>${step.label.endsWith(":bulk") ? "① 体相优化" : step.label.endsWith(":surface") ? "② 表面优化" : step.label.endsWith(":relax") ? "① 结构优化" : "② 单点计算"} · #${step.pk} · ${esc(step.process_state)}${step.exit_status !== null ? ` · 退出码 ${step.exit_status}` : ""}</p>`).join("")}
    ${job.summary && job.workflow !== "bulk_surface" ? `<p>流程汇总：单点能 ${esc(String(job.summary.singlepoint_energy_ev))} eV；最大力 ${esc(String(job.summary.max_force_ev_per_angstrom))} eV/Å</p>` : ""}
    ${job.summary && job.workflow === "bulk_surface" ? `<p>体相晶格常数 ${esc(String(job.summary.lattice_constant_angstrom))} Å · ${job.summary.layers} 层 / 固定 ${job.summary.fixed_layers} 层 · 自由原子最大力 ${esc(String(job.summary.max_free_force_ev_per_angstrom))} eV/Å</p>` : ""}
    ${recovery ? `<p>Agent 处理：${esc(recoveryLabels[recovery.state] || recovery.state)}${recovery.decision?.decision?.reason ? ` · ${esc(recovery.decision.decision.reason)}` : ""}${recovery.result?.reason ? ` · ${esc(recovery.result.reason)}` : ""}</p>` : ""}
    <small>AiiDA 记录同步于 ${esc(job.checked_at ? new Date(job.checked_at).toLocaleTimeString() : "等待后台同步")}</small>`;
}

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
      const job = submission ? (records.live_jobs?.[submission.job.id] || submission.job) : null;
      const resource = item.resources;
      return `<article class="calculation-preview"><div class="calculation-card-heading"><strong>${esc(item.structure.formula)} · ${item.workflow === "bulk_surface" ? "体相 → 切表面 → 表面优化" : item.workflow === "relax_static" ? "优化 → 单点流程" : "计算任务"}</strong><span>${job ? `AiiDA #${job.pk}` : "等待确认"}</span></div>
        ${item.workflow === "bulk_surface" ? "<p>流程已确认后由 AiiDA 自动推进；失败时通知并诊断，重试需重新预览确认。</p>" : ""}
        ${item.workflow_note ? `<p>${esc(item.workflow_note)} · 最多 ${item.max_calculations} 个计算，累计时限 ${item.total_wallclock_limit_seconds} 秒。</p>` : ""}
        ${item.correction ? `<p class="result-source">${esc(item.correction)}</p>` : ""}
        <p>${esc(item.server.label)} · ${esc(resource.queue)} · ${resource.nodes} 节点 × ${resource.ranks_per_node} MPI · ${resource.gpus_per_node || 0} ${esc(resource.gpu_type || "GPU")} / 节点 · ${resource.wallclock_seconds} 秒 · ${resource.memory_mb} MB</p>
        ${item.protocol ? `<p>方案：${esc(item.protocol.id)} · ${esc(item.protocol.description)}</p>` : ""}
        <p class="result-source">预览 ${esc(item.preview_id)}<br>输入摘要 ${esc(item.input_sha256)}</p>
        <details><summary>查看实际参数、结构和提交脚本</summary>${Object.entries(item.files).map(([name, content]) => `<h4>${esc(name)}</h4><pre>${esc(content)}</pre>`).join("")}</details>
        ${job ? `<div class="live-task-state" data-job-live="${esc(job.id)}">${renderTaskStatus(job, records.recovery?.[job.id])}</div><button type="button" data-refresh-job="${esc(job.id)}">查看完整任务记录</button>
        ${job.finished_ok && job.output_structure ? `<button type="button" data-import-job="${esc(job.id)}">取回优化结构用于建模</button>` : ""}
        ${!job.finished_ok ? `<button type="button" data-recovery-job="${esc(job.id)}" data-recovery-mode="diagnose">唤起 Agent 分析</button><button type="button" data-recovery-job="${esc(job.id)}" data-recovery-mode="off">关闭自动处理</button>` : ""}` : `${item.workflow === "bulk_surface" ? "" : `<label class="recovery-choice"><input type="checkbox" data-auto-recovery="${esc(item.preview_id)}" ${item.workflow === "bulk_surface" ? "disabled" : ""}>允许 Agent 自动修复最多 2 次；每次不超过原时限 2 倍（含原任务最多 5 倍累计时限），不增加内存、CPU/GPU，不改变物理模型。未勾选时只诊断并给出预览。</label>`}<button type="button" class="tool-submit" data-confirm-preview="${esc(item.preview_id)}">确认${item.workflow && item.workflow !== "single" ? "整个流程" : "以上参数"}并提交</button>`}
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
        const approved = panel.querySelector(`[data-auto-recovery="${CSS.escape(target.dataset.confirmPreview)}"]`)?.checked;
        await api(`${path(id)}/confirm/${encodeURIComponent(target.dataset.confirmPreview)}`, { method: "POST", body: JSON.stringify({mode: approved ? "auto" : "diagnose", max_attempts: 2}) });
        if (id === getSession()) await onConfirmed(id);
      } else if (target.dataset.importJob) {
        await api(`${path(id)}/jobs/${encodeURIComponent(target.dataset.importJob)}/structure`, {method: "POST"});
        await refresh();
        toast("优化结构已加入本次对话，可继续建模。");
      } else if (target.dataset.recoveryJob) {
        await api(`${path(id)}/jobs/${encodeURIComponent(target.dataset.recoveryJob)}/recovery`, { method: "POST", body: JSON.stringify({mode: target.dataset.recoveryMode, max_attempts: 2}) });
        toast(target.dataset.recoveryMode === "off" ? "已关闭此任务后续自动处理。" : "已请求后台 Agent 分析，结果将显示在通知与任务卡片中。");
      } else if (target.dataset.refreshJob) {
        const result = await api(`/api/automation/jobs/${encodeURIComponent(target.dataset.refreshJob)}`);
        if (id === getSession()) target.parentElement.querySelector(".job-live-result").textContent = (result.submission_state === "rejected_by_scheduler" ? `调度器拒绝提交，尚未排队：${result.recent_process_errors?.[0]?.message || "请检查资源配置"}\n\n` : "") + JSON.stringify(result, null, 2);
      } else if (target.id === "refresh-calculations") await refresh();
    } catch (error) { toast(error.message); }
    finally { pending = false; target.disabled = false; }
  });

  let polling = false;
  async function refreshLive() {
    const id = getSession();
    if (!enabled || !id || pending || polling || document.hidden) return;
    polling = true;
    try {
      const data = await api(`${path(id)}/events`);
      if (id !== getSession() || !records) return;
      records.live_jobs = data.jobs; records.recovery = data.recovery;
      for (const element of panel.querySelectorAll("[data-job-live]")) {
        const job = data.jobs[element.dataset.jobLive];
        if (job) element.innerHTML = renderTaskStatus(job, data.recovery?.[job.id]);
      }
      const known = new Set(Object.values(records.submissions).map((value) => value.job.id));
      const previewIds = new Set(records.previews.map((value) => value.preview_id));
      if (Object.keys(data.jobs).some((id) => !known.has(id)) || Object.values(data.recovery || {}).some((value) => value?.result?.preview_id && !previewIds.has(value.result.preview_id))) await refresh();
    } catch { /* Notification panel shows stale-monitor warnings; do not spam toasts. */ }
    finally { polling = false; }
  }
  setInterval(refreshLive, 15000);

  return {
    refresh,
    enabled(value) { enabled = value; button.hidden = !enabled; if (!enabled) { selected = []; records = null; drawAttachments(); render(); } },
    selectedIds() { return selected.map((item) => item.id); },
    clearSelection() { selected = []; drawAttachments(); },
    reset() { selected = []; records = null; session = null; drawAttachments(); render(); },
    busy() { return pending; },
  };
}
