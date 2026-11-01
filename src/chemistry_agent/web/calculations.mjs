import {workflowPlan} from "./workflow-plan.mjs";
import { escapeHtml as esc } from './core.mjs';
import { structureButton, getFileBlob, isStructureName } from './structure-links.mjs';

const progressSignature = job => JSON.stringify([job.adsorption,job.steps?.map(s=>[s.pk,s.branch_valid,s.scheduler_state,s.scheduler_job_id,s.process_state,s.paused,s.finished_ok,s.results?.run_status])]);
const number = Number.isFinite;
const stateNames = {completed:'计算完成', failed:'计算失败', unconverged:'未收敛', needs_review:'结果待核对', submission_rejected:'提交被拒绝', paused:'已暂停', running:'运行中', queued:'排队中', preparing:'准备中'};
const stepName = label => ({bulk:'体相优化',surface:'表面优化',relax:'结构优化',singlepoint:'单点计算'})[label?.split(':').at(-1)] || '计算';
const fact = (label, value) => value == null || value === '' ? '' : `<p>${esc(label)}：${esc(String(value))}</p>`;
const convergence = value => value === true ? '通过' : value === false ? '未通过' : null;
export function renderTaskStatus(job, recovery = null) {
  const title = stateNames[job.display_state] || job.display_title || (job.finished_ok ? '计算完成' : job.process_status || job.process_state || '正在同步');
  const result = job.results || {}, summary = job.summary || {}, run = result.run_status || {};
  const energy = result.total_energies?.energy_extrapolated;
  const recoveryNames = {queued:'等待分析', running:'正在分析', diagnosed:'已诊断', needs_user:'需要你处理', submitting:'恢复提交中', submitted:'已提交恢复任务', awaiting_confirmation:'等待确认修复', interrupted:'分析中断', disabled:'已关闭'};
  return `<details class="task-status" data-fold="status"><summary><strong>${esc(title)}</strong></summary><div>${fact('AiiDA',`#${job.pk}`)}${fact('调度器',job.scheduler_job_id)}${fact('退出码',job.exit_status)}${fact('总能量',number(energy) ? `${energy} eV` : null)}${fact('电子收敛',convergence(run.electronic_converged))}${run.nsw > 0 ? fact('结构收敛',convergence(run.ionic_converged)) : ''}${fact('体相晶格常数',number(summary.lattice_constant_angstrom) ? `${summary.lattice_constant_angstrom} Å` : null)}${fact('自由原子最大力',number(summary.max_free_force_ev_per_angstrom) ? `${summary.max_free_force_ev_per_angstrom} eV/Å` : null)}${(job.steps || []).map(step => fact(stepName(step.label), `#${step.pk} · ${step.finished_ok ? '完成' : step.process_state}`)).join('')}${!job.finished_ok ? fact('错误',job.recent_process_errors?.[0]?.message || job.exit_message) : ''}${recovery ? fact('Agent 处理',[recoveryNames[recovery.state] || recovery.state,recovery.result?.reason || recovery.decision?.decision?.reason].filter(Boolean).join(' · ')) : ''}</div></details>`;
}

export function renderTaskCard(item, job, recovery, sessionPath, policy={}) {
  if (item.replaced_by) return '';
  const id = esc(item.preview_id), surface = item.surface_plan, request = item.request || {};
  const stage = item.stages?.find(s => s.stage === 'surface');
  const incar = stage?.incar || request.incar || {}, mesh = stage?.kmesh || request.kmesh;
  const params = [incar.ENCUT != null && `ENCUT ${incar.ENCUT} eV`, Array.isArray(mesh) && `k ${mesh.join('×')}`, incar.ISIF != null && `ISIF ${incar.ISIF}`].filter(Boolean).join(' · ');
  const r = item.resources || {}, suffix = surface?.co_sites?.length ? 'CO 吸附能' : item.workflow === 'bulk_surface' ? '表面优化' : item.workflow === 'relax_static' ? '优化 → 单点' : (incar.NSW > 0 ? '结构优化' : '计算任务');
  const files = Object.entries(item.files || {}).map(([name,content], index) => `<details data-fold="file-${index}"><summary>${esc(name)}</summary>${/(^|\/)(POSCAR|CONTCAR)$/i.test(name) ? `<button type="button" data-view-input="${index}">侧栏查看结构</button>` : ''}<pre>${esc(content)}</pre></details>`).join('');
  const resultActions = job?.finished_ok && job.output_structure ? `<div class="task-actions">${structureButton('job',job.id,`optimized-job-${job.pk}.extxyz`,surface?.co_sites?.length?'查看洁净表面':'查看优化结构')}<a href="${sessionPath}/jobs/${encodeURIComponent(job.id)}/structure.extxyz" download>${surface?.co_sites?.length?'下载洁净表面':'下载优化结构'}</a></div>` : '';
  return `<article class="calculation-preview" data-task="${id}"><header><strong>${esc(item.structure.formula)} · ${suffix}</strong></header>${params ? `<p class="task-parameters">${esc(params)}</p>` : ''}
    ${job ? renderTaskStatus(job,recovery) : '<strong class="task-pending">待确认</strong>'}${resultActions}
    ${workflowPlan(item,job,sessionPath)}
    <details data-fold="inputs"><summary>参数、结构与提交脚本</summary>${surface ? fact('表面',`${surface.size.join('×')} · ${surface.layers} 层 · 固定底部 ${surface.fixed_layers} 层 · 真空 ${surface.vacuum_angstrom} Å`) : ''}${surface?.bulk_job_id ? fact('复用体相',`#${item.stages?.[0]?.reused_pk || surface.bulk_job_id}`) : ''}${fact('资源',[item.server?.label,r.queue,r.nodes && `${r.nodes} 节点`,r.gpus_per_node ? `${r.gpus_per_node} ${r.gpu_type || 'GPU'}/节点` : null,r.wallclock_seconds && `${r.wallclock_seconds} 秒`].filter(Boolean).join(' · '))}${item.max_calculations ? fact('计算上限',`${item.max_calculations} 次 · 总时限 ${item.total_wallclock_limit_seconds} 秒`) : ''}${item.surface_structure_id || item.structure_id ? structureButton('artifact',item.surface_structure_id || item.structure_id,'input.extxyz','查看输入结构') : ''}${files}</details>
    ${job ? (!job.finished_ok ? `<details data-fold="recovery"><summary>故障处理</summary>${surface && !surface.co_sites?.length ? '' : `<label>自动重试上限 <input type="number" min="0" max="5" value="${esc(policy.max_attempts ?? 2)}" data-retry-limit="${id}"></label><button data-recovery-job="${esc(job.id)}" data-recovery-mode="auto">启用或更新自动重试</button>`}<button data-recovery-job="${esc(job.id)}" data-recovery-mode="diagnose">请求分析</button><button data-recovery-job="${esc(job.id)}" data-recovery-mode="off">关闭自动处理</button></details>` : '') : `${surface && !surface.co_sites?.length ? '' : `<details data-fold="policy"><summary>重试设置</summary><label><input type="checkbox" data-auto-recovery="${id}">自动修复</label><label>最多 <input type="number" min="0" max="5" value="2" data-retry-limit="${id}"> 次</label></details>`}<button type="button" class="tool-submit" data-confirm-preview="${id}">${item.workflow && item.workflow !== 'single' ? '确认整个流程并提交' : '确认提交'}</button>`}</article>`;
}

export function createCalculations({ api, getSession, ensureSession, isBusy, toast, onConfirmed, viewer, chatFiles, syncReport, reviseStep }) {
  const panel = document.getElementById("calculation-cards");
  const attachments = document.getElementById("structure-attachments");
  const input = document.getElementById("structure-file");
  const button = document.getElementById("structure-upload");
  let enabled = false, session = null, selected = [], records = null, pending = false;
  const path = (id) => `/api/automation/chat/${encodeURIComponent(id)}`;

  function drawAttachments() {
    attachments.innerHTML = selected.map((item) => `<span class="structure-chip">${structureButton('artifact',item.id,`${item.name}.extxyz`,`${item.name} · ${item.formula}`)} <button type="button" data-remove-structure="${esc(item.id)}" aria-label="移除结构附件">×</button></span>`).join("");
  }

  const structureList = document.getElementById('structure-list');
  const unique = (items, key) => [...new Map(items.map(item => [key(item), item])).values()];
  function drawStructures() {
    const base = path(session);
    const structures = unique(records?.structures || [], item => item.source_structure_uuid || item.id);
    const jobs = Object.values(records?.live_jobs || {}).filter(job => job.finished_ok && job.output_structure && !structures.some(item => item.source_job_id === job.id));
    const row = (title, kind, id, name, url, attach='') => `<div class="structure-item"><span>${esc(title)}</span>${structureButton(kind,id,name,'查看')}<a href="${esc(url)}" download>下载</a>${attach}</div>`;
    const rows = structures.map(item => row(`${item.name} · ${item.formula}`, 'artifact', item.id, `${item.name}.extxyz`, `${base}/files/structure/${encodeURIComponent(item.id)}`, `<button data-attach-structure="${esc(item.id)}">用于对话</button>`));
    for (const job of jobs) rows.unshift(row(`优化结构 · #${job.pk}`, 'job', job.id, `optimized-job-${job.pk}.extxyz`, `${base}/jobs/${encodeURIComponent(job.id)}/structure.extxyz`, `<button data-import-job="${esc(job.id)}">用于对话</button>`));
    for (const file of unique(chatFiles().filter(file => isStructureName(file.name)), file => file.id)) rows.push(row(file.name,'file',file.id,file.name,`/api/files/${encodeURIComponent(file.id)}`));
    structureList.innerHTML = rows.join('') || '<p class="empty-result">本对话暂无结构</p>';
    structureList.innerHTML += (records?.models || []).map(item => `<details class="model-record"><summary>建模代码 · ${esc(item.name)}</summary><pre>${esc(item.code)}</pre><a href="${base}/files/model/${esc(item.id)}" download>下载 model.py</a></details>`).join('');
  }
  function render() {
    drawStructures();
    const savedFolds = new Set([...panel.querySelectorAll('details[open]')].map(d => `${d.closest('[data-task]')?.dataset.task}:${d.dataset.fold}`));
    const approved = new Set([...panel.querySelectorAll('[data-auto-recovery]:checked')].map(el => el.dataset.autoRecovery));
    const limits = new Map([...panel.querySelectorAll("[data-retry-limit]")].map(e => [e.dataset.retryLimit,e.value]));
    panel.hidden = !enabled;
    const cards = (records?.previews || []).map(item => {
      const ref = records.submissions[item.preview_id]?.job;
      const job = ref ? (records.live_jobs?.[ref.id] || ref) : null;
      return renderTaskCard(item, job, records.recovery?.[ref?.id], path(session),records.policies?.[ref?.id] || {});
    }).join('');
    panel.innerHTML = cards || '<p class="empty-result">本对话暂无计算任务</p>';
    for (const e of panel.querySelectorAll("[data-retry-limit]")) if (limits.has(e.dataset.retryLimit)) e.value = limits.get(e.dataset.retryLimit);
    for (const d of panel.querySelectorAll('details')) d.open = savedFolds.has(`${d.closest('[data-task]')?.dataset.task}:${d.dataset.fold}`);
    for (const el of panel.querySelectorAll('[data-auto-recovery]')) el.checked = approved.has(el.dataset.autoRecovery);
  }

  function openStructure(kind, id, name) {
    if (!/^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/i.test(id)) return;
    const base = path(getSession());
    const url = kind === 'file' ? `/api/files/${id}` : kind === 'artifact' ? `${base}/files/structure/${id}` : kind === 'job' ? `${base}/jobs/${id}/structure.extxyz` : null;
    if (url) return viewer.open(name, () => getFileBlob(url));
  }
  document.addEventListener('click', event => {
    const target = event.target.closest('button[data-open-kind]');
    if (target) openStructure(target.dataset.openKind, target.dataset.openId, target.dataset.openName);
  });
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
  async function onAction(event) {
    const target = event.target.closest("button");
    if (!target || target.dataset.openKind || isBusy() || pending) return;
    if (target.dataset.stepOutput) {
      const url = path(getSession())+'/jobs/'+encodeURIComponent(target.dataset.parentJob)+'/steps/'+encodeURIComponent(target.dataset.stepOutput)+'/structure.extxyz';
      return viewer.open("optimized-step.extxyz",()=>getFileBlob(url));
    }
    if (target.dataset.reviseStep) {
      const id = target.closest("[data-task]").dataset.task;
      reviseStep(id,target.dataset.reviseStep); return;
    }
    if (target.dataset.viewInput !== undefined) {
      const item = records.previews.find(entry => entry.preview_id === target.closest('[data-task]').dataset.task);
      const [name, content] = Object.entries(item.files)[Number(target.dataset.viewInput)];
      return viewer.open(name.split('/').at(-1), () => new Blob([content], {type:'text/plain'}));
    }
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
        const control = target.closest("[data-task]").querySelector("[data-retry-limit]");
        if (control && (!control.value || !control.reportValidity())) return;
        const limit = control ? Number(control.value) : 0;
        await api(`${path(id)}/confirm/${encodeURIComponent(target.dataset.confirmPreview)}`, { method: "POST", body: JSON.stringify({mode: approved && limit ? "auto" : "diagnose", max_attempts: approved ? limit : 2}) });
        if (id === getSession()) await onConfirmed(id);
      } else if (target.dataset.importJob) {
        const imported = await api(`${path(id)}/jobs/${encodeURIComponent(target.dataset.importJob)}/structure`, {method: "POST"});
        if (id !== getSession()) return;
        await refresh();
        selected = [...selected.filter(item => item.id !== imported.id), imported].slice(-8); drawAttachments();
        toast("优化结构已附加到输入框。");
      } else if (target.dataset.recoveryJob) {
        const control = target.closest("[data-task]").querySelector("[data-retry-limit]");
        if (control && (!control.value || !control.reportValidity())) return;
        const limit = control ? Number(control.value) : 2;
        const mode = target.dataset.recoveryMode === "auto" && !limit ? "diagnose" : target.dataset.recoveryMode;
        await api(`${path(id)}/jobs/${encodeURIComponent(target.dataset.recoveryJob)}/recovery`, { method:"POST", body:JSON.stringify({mode,max_attempts:limit}) });
        if (mode==="auto") { toast("已更新自动重试上限："+limit+" 次"); return; }
        toast(target.dataset.recoveryMode === "off" ? "已关闭此任务后续自动处理。" : "已请求后台 Agent 分析，结果将显示在通知与任务卡片中。");
      }
    } catch (error) { toast(error.message); }
    finally { pending = false; target.disabled = false; }
  }
  panel.addEventListener('click', onAction);
  structureList.addEventListener('click', onAction);

  let polling = false;
  async function refreshLive() {
    const id = getSession();
    if (!enabled || !id || pending || polling || document.hidden) return;
    polling = true;
    try {
      const data = await api(`${path(id)}/events`);
      if (id !== getSession() || !records) return;
      if (data.latest_report) await syncReport(id,data.latest_report);
      if (id !== getSession() || !records) return;
      const changes = Object.entries(data.jobs).some(([id, job]) => {
        const previous = records.live_jobs?.[id];
        return !previous || progressSignature(previous) !== progressSignature(job) || renderTaskStatus(previous, records.recovery?.[id]) !== renderTaskStatus(job, data.recovery?.[id]) || Boolean(previous.output_structure) !== Boolean(job.output_structure);
      });
      records.live_jobs = data.jobs; records.recovery = data.recovery;
      if (changes) render();
      const known = new Set(Object.values(records.submissions).map((value) => value.job.id));
      const previewIds = new Set(records.previews.map((value) => value.preview_id));
      if (Object.keys(data.jobs).some((id) => !known.has(id)) || Object.values(data.recovery || {}).some((value) => value?.result?.preview_id && !previewIds.has(value.result.preview_id))) await refresh();
    } catch { /* Notification panel shows stale-monitor warnings; do not spam toasts. */ }
    finally { polling = false; }
  }
  setInterval(refreshLive, 15000);

  return {
    refresh, drawStructures,
    enabled(value) { enabled = value; button.hidden = !enabled; if (!enabled) { selected = []; records = null; drawAttachments(); render(); } },
    selection() { return [...selected]; },
    selectedIds() { return selected.map((item) => item.id); },
    clearSelection() { selected = []; drawAttachments(); },
    reset() { selected = []; records = null; session = null; drawAttachments(); render(); },
    busy() { return pending; },
  };
}
