import {mountMaps,captureMaps} from './plan-map.mjs';
import {planFigures} from './result-figures.mjs';
import {createStepDetail} from "./step-detail.mjs?v=20261102-general-plans";
import {workflowPlan} from "./workflow-plan.mjs";
import {renderGeneralPlan} from './general-plan.mjs';
import {drawPlanAttention} from './plan-attention.mjs';
import { escapeHtml as esc } from './core.mjs';
import { structureButton, getFileBlob, isStructureName } from './structure-links.mjs';
import {fileIcon,updateStructureCount} from './file-actions.mjs';
import {t} from './i18n.mjs';

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

export function createCalculations({ api, getSession, isBusy, toast, onConfirmed, viewer, openAttachment, chatFiles, syncReport, refreshProgress, onPlans, reviseStep }) {
  const panel = document.getElementById("calculation-cards");
  const status = document.getElementById("plan-status");
  const knownSteps = new Map();
  function displayCount(preview,key,count) {
    const target = panel.querySelector(`[data-task="${CSS.escape(preview)}"] [data-open-step="${CSS.escape(key)}"] [data-ionic-step]`);
    if (target && count!=null) target.textContent = ' · '+count+' 步';
  }
  function rememberCount(id,key,count) {
    knownSteps.set(id+'/'+key,{count,checked:Date.now()});
    displayCount(id,key,count);
  }
  const detail = createStepDetail({api,reviseStep,rememberCount,onAttentionChange:refreshAttention});
  const attachments = document.getElementById("structure-attachments");
  let selectedPlan = null, selectedPlanSession = null;
  panel.addEventListener('change', event=>{if(event.target.matches('[data-plan-select]')){selectedPlan=event.target.value;try{sessionStorage.setItem('chemistry.selectedPlan.'+session,selectedPlan);}catch{}detail.close();render();}});
  let enabled = false, session = null, selected = [], records = null, pending = false;
  const path = (id) => `/api/automation/chat/${encodeURIComponent(id)}`;
  let attention = {url:'',items:null,error:''}, attentionRequest;

  function attentionUrl() {
    const plan = records?.plans?.find(item => item.id === selectedPlan);
    return session && plan && !plan.read_only ? `${path(session)}/workflows/${plan.id}/attention` : '';
  }
  async function refreshAttention() {
    const url = attentionUrl();
    if (!enabled || !url || attentionRequest?.url === url || document.hidden
      || !panel.closest('[role="tabpanel"]')?.getClientRects().length) return;
    const request = {url};
    attentionRequest = request;
    try {
      const data = await api(url);
      if (attentionRequest !== request || url !== attentionUrl() || session !== getSession()) return;
      attention = {url,items:data.items,error:''};
    } catch (error) {
      if (attentionRequest !== request || url !== attentionUrl() || session !== getSession()) return;
      attention.error = error.message;
    } finally {
      if (attentionRequest === request) attentionRequest = null;
    }
    drawPlanAttention(panel, attention);
    detail.updateAttention(selectedPlan, attention.items || []);
  }
  setInterval(refreshAttention, 4000);

  function drawAttachments() {
    attachments.innerHTML = selected.map((item) => `<span class="structure-chip">${structureButton('artifact',item.id,`${item.name}.extxyz`,`${item.name} · ${item.formula}`)} <button type="button" data-remove-structure="${esc(item.id)}" aria-label="移除结构附件"><svg class="icon" aria-hidden="true"><use href="/assets/ui-icons.svg#i-close"/></svg></button></span>`).join("");
  }

  const structureList = document.getElementById('structure-list');
  const unique = (items, key) => [...new Map(items.map(item => [key(item), item])).values()];
  function drawStructures() {
    const base = path(session);
    const structures = unique(records?.structures || [], item => item.source_structure_uuid || item.id);
    const jobs = Object.values(records?.live_jobs || {}).filter(job => job.finished_ok && job.output_structure && !structures.some(item => item.source_job_id === job.id));
    const cite=(attr,id,name)=>`<button type="button" class="file-action" ${attr}="${esc(id)}" title="用于对话" aria-label="用于对话：${esc(name)}">${fileIcon('cite')}</button>`;
    const row = (title, kind, id, name, url, attach='') => `<div class="structure-item"><span title="${esc(title)}">${esc(title)}</span><span class="workspace-file-actions">${structureButton(kind,id,name,'查看',true)}<a class="file-action" href="${esc(url)}" download title="下载" aria-label="下载：${esc(name)}">${fileIcon('download')}</a>${attach}</span></div>`;
    const rows = structures.map(item => row(`${item.name} · ${item.formula}`, 'artifact', item.id, `${item.name}.extxyz`, `${base}/files/structure/${encodeURIComponent(item.id)}`, cite('data-attach-structure',item.id,item.name)));
    for (const job of jobs) rows.unshift(row(`优化结构 · #${job.pk}`, 'job', job.id, `optimized-job-${job.pk}.extxyz`, `${base}/jobs/${encodeURIComponent(job.id)}/structure.extxyz`, cite('data-import-job',job.id,`优化结构 #${job.pk}`)));
    for (const file of unique(chatFiles().filter(file => isStructureName(file.name)), file => file.id)) rows.push(row(file.name,'file',file.id,file.name,`/api/files/${encodeURIComponent(file.id)}`));
    structureList.innerHTML = rows.join('');
    structureList.innerHTML += (records?.models || []).map(item => `<details class="model-record"><summary>建模代码 · ${esc(item.name)}</summary><pre>${esc(item.code)}</pre><a href="${base}/files/model/${esc(item.id)}" download>下载 model.py</a></details>`).join('');
    updateStructureCount();
  }
  function drawReportFigures() {
    if (!session || session !== getSession()) return;
    for (const element of document.querySelectorAll('[data-plan-report^="plan:"]')) {
      const [,id,revision] = element.dataset.planReport.split(':');
      const plan = records?.plans?.find(row=>row.id===id && String(row.revision)===revision);
      if (plan) {
        const html = planFigures(path(session),plan);
        if (element.dataset.rendered !== html) {
          element.innerHTML=html; element.dataset.rendered=html;
        }
      }
    }
  }
  // Do not replace a card under the pointer or an active search during a live update.
  let deferredRender=false;
  panel.addEventListener('mapidle',()=>{if(deferredRender) requestAnimationFrame(()=>{if(deferredRender) render();});});
  function render() {
    if(panel.querySelector('[data-gesture],.map-pop:not([hidden]),[data-task-agent]:focus')) {deferredRender=true;return;}
    deferredRender=false;
    drawReportFigures();
    drawStructures();
    const savedFolds = new Set([...panel.querySelectorAll('details[open]')].map(d => `${d.closest('[data-task]')?.dataset.task}:${d.dataset.fold}`));
    const approved = new Set([...panel.querySelectorAll('[data-auto-recovery]:checked')].map(el => el.dataset.autoRecovery));
    const limits = new Map([...panel.querySelectorAll("[data-retry-limit]")].map(e => [e.dataset.retryLimit,e.value]));
    const plans=records?.plans || [];
    if(selectedPlanSession!==session){selectedPlanSession=session;selectedPlan=null;try{selectedPlan=sessionStorage.getItem('chemistry.selectedPlan.'+session);}catch{}}
    if(!plans.some(plan=>plan.id===selectedPlan)) selectedPlan=plans[0]?.id;
    for(const plan of plans) detail.updateGeneral(plan);
    const activePlan=plans.find(plan=>plan.id===selectedPlan);
    const url = attentionUrl();
    if (attention.url !== url) attention = {url,items:null,error:''};
    const picker=plans.length>1?`<div class="plan-selector"><select data-plan-select aria-label="选择方案">${plans.map(plan=>`<option data-no-i18n value="${esc(plan.id)}" ${plan.id===selectedPlan?'selected':''}>${esc(plan.title)}</option>`).join('')}</select></div>`:'';
    const graphs=activePlan?picker+renderGeneralPlan(activePlan,records?.task_agent_presets):'';
    panel.classList.toggle('has-plan-surface', Boolean(activePlan));
    const cards = (records?.previews || []).filter(item=>!item.plan_id).map(item => {
      const ref = records.submissions[item.preview_id]?.job;
      const job = ref ? (records.live_jobs?.[ref.id] || ref) : null;
      detail.update(item,job);
      return renderTaskCard(item, job, records.recovery?.[ref?.id], path(session),records.policies?.[ref?.id] || {});
    }).join('');
    const panelTop = panel.parentElement.parentElement.scrollTop;
    const focusedAttention = panel.contains(document.activeElement)
      && (document.activeElement.dataset.attentionItem || document.activeElement.matches('[data-plan-view="attention"]'));
    const mapViews = captureMaps(panel,{suspend:true,dispose:true});
    const markup = graphs + (cards && activePlan ? `<details class="legacy-calculations"><summary>其他计算记录</summary>${cards}</details>` : cards);
    panel.hidden = !enabled || !markup;
    panel.innerHTML = markup;
    mountMaps(panel,mapViews);
    drawPlanAttention(panel, attention);
    if (focusedAttention) {
      const row = typeof focusedAttention === 'string'
        && panel.querySelector(`[data-attention-item="${CSS.escape(focusedAttention)}"]`);
      (row || panel.querySelector('[data-plan-view="attention"]'))?.focus({preventScroll:true});
    }
    void refreshAttention();
    panel.parentElement.parentElement.scrollTop = panelTop;
    for (const [id,value] of knownSteps) displayCount(...id.split('/'),value.count);
    for (const e of panel.querySelectorAll("[data-retry-limit]")) if (limits.has(e.dataset.retryLimit)) e.value = limits.get(e.dataset.retryLimit);
    for (const d of panel.querySelectorAll('details')) d.open = savedFolds.has(`${d.closest('[data-task]')?.dataset.task}:${d.dataset.fold}`);
    for (const el of panel.querySelectorAll('[data-auto-recovery]')) el.checked = approved.has(el.dataset.autoRecovery);
  }

  panel.addEventListener('focusout', event=>{
    if(event.target.matches('[data-task-agent]') && deferredRender) requestAnimationFrame(render);
  });
  panel.addEventListener('change', async event=>{
    const select=event.target.closest('[data-task-agent]');
    if(!select || pending) return;
    const id=getSession(), node=select.dataset.taskAgent;
    const plan=records.plans.find(item=>item.id===select.closest('[data-general-plan]').dataset.generalPlan);
    pending=true;
    select.disabled=true;
    try {
      const [presetId, version] = select.value.split(':');
      await api(`${path(id)}/workflows/${plan.id}/steps/${encodeURIComponent(node)}/agent`, {
        method:'PUT',body:JSON.stringify({expected_revision:plan.revision,preset_id:presetId || null,preset_version:version ? Number(version) : null}),
      });
      if(id===getSession()) {
        const view=select.closest('.plan-kanban,.map-world')?.classList.contains('plan-kanban')?'plan-kanban':'map-world';
        await refresh();
        panel.querySelector(`.${view} [data-task-agent="${CSS.escape(node)}"]`)?.focus({preventScroll:true});
      }
    } catch(error) {
      select.value=select.dataset.assignment;
      toast(error.message);
      if(id===getSession()) await refresh().catch(()=>{});
    } finally {pending=false;select.disabled=false;}
  });

  function openStructure(kind, id, name) {
    if (!/^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/i.test(id)) return;
    if (kind === 'file') return openAttachment(id, name);
    const base = path(getSession());
    const url = kind === 'artifact' ? `${base}/files/structure/${id}` : kind === 'job' ? `${base}/jobs/${id}/structure.extxyz` : null;
    if (url) return viewer.open(name, () => getFileBlob(url));
  }
  document.addEventListener('click', event => {
    const target = event.target.closest('button[data-open-kind]');
    if (target) openStructure(target.dataset.openKind, target.dataset.openId, target.dataset.openName);
  });
  async function refresh() {
    const id = getSession();
    if (!enabled || !id) {
      records = null;
      session = id;
      status.textContent = '暂无计算方案';
      onPlans?.(id,[]);
      render();
      return;
    }
    if (session !== id) { selected = []; drawAttachments(); }
    session = id;
    const response = await api(`${path(id)}/artifacts`).catch(error => {
      if (id === getSession() && !records) status.textContent = '未能读取计算方案。';
      throw error;
    });
    if (id !== getSession()) return;
    records = response;
    status.textContent = '暂无计算方案';
    onPlans?.(id,records.plans || []);
    void refreshProgress?.(id);
    render();
  }

  attachments.addEventListener("click", (event) => {
    const target = event.target.closest("[data-remove-structure]");
    if (target && !isBusy()) { selected = selected.filter((item) => item.id !== target.dataset.removeStructure); drawAttachments(); }
  });
  async function onAction(event) {
    const target = event.target.closest("button");
    if (!target || target.dataset.openKind) return;
    if (target.dataset.planView === 'attention') {void refreshAttention();return;}
    if (target.closest('.map-board') && !target.matches('.map-node,.kanban-card,[data-confirm-plan],[data-control-plan],[data-revise-plan],[data-attention-item]')) return;
    if(target.dataset.generalStep || target.dataset.attentionItem) {
      const plan=records?.plans.find(p=>p.id===target.closest('[data-general-plan]').dataset.generalPlan);
      const items=attention.items || [];
      const item=items.find(row=>(row.approval_id || row.job_id)===target.dataset.attentionItem);
      const node=plan?.nodes.find(n=>n.id===(item?.node_id || target.dataset.generalStep));
      if(node) {
        detail.openGeneral(plan,node,path(session),items,item);
        if (attention.items === null) void refreshAttention();
      }
      return;
    }
    if(target.dataset.revisePlan) {
      const plan=records.plans.find(p=>p.id===target.dataset.revisePlan);
      reviseStep(plan.id,plan.title,plan.revision); return;
    }
    if (target.dataset.openStep) {
      const item = records?.previews.find(p=>p.preview_id===target.closest('[data-task]').dataset.task);
      if (item) {
        const submitted=records.submissions[item.preview_id]?.job;
        detail.open(item,records.live_jobs?.[submitted?.id]||submitted,target.dataset.openStep,path(session));
      }
      return;
    }
    if (isBusy() || pending) return;
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
      selected = [...selected.filter((value) => value.id !== item.id), item];
      drawAttachments(); return;
    }
    if (target.dataset.controlPlan) {
      const message = target.dataset.planAction==='pause'
        ? '暂停后续步骤？已提交的作业将继续运行。'
        : '继续执行方案？后续步骤将按依赖启动。';
      if (!confirm(t(message))) return;
    }
    pending = true; target.disabled = true;
    const id = getSession();
    try {
      if(target.dataset.confirmPlan || target.dataset.controlPlan) {
        const plan=records.plans.find(p=>p.id===(target.dataset.confirmPlan || target.dataset.controlPlan));
        const action=target.dataset.confirmPlan?'confirm':'control';
        await api(`${path(id)}/workflows/${plan.id}/${action}`,{method:'POST',body:JSON.stringify(action==='confirm'?{revision:plan.revision}:{action:target.dataset.planAction})});
        if(id===getSession()) await refresh();
        toast(action==='confirm'?'方案已确认，后台将按依赖执行。':target.dataset.planAction==='pause'?'已暂停启动后续步骤，已提交作业继续运行。':'已恢复方案执行。');
      } else if (target.dataset.confirmPreview) {
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
        selected = [...selected.filter(item => item.id !== imported.id), imported]; drawAttachments();
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

  async function refreshVisibleCount(sessionId) {
    if (!panel.getClientRects().length) return;
    const candidates = [];
    for (const item of records.previews) {
      const ref = records.submissions[item.preview_id]?.job;
      const job = records.live_jobs?.[ref?.id];
      if (!job || job.finished_ok || job.exit_status!=null) continue;
      for (const child of job.steps || [job]) {
        if (child.scheduler_state!=='running' || child.exit_status!=null || child.paused) continue;
        const key = job.steps ? child.label.split(':').at(-1) : 'single';
        const last = knownSteps.get(item.preview_id+'/'+key)?.checked || 0;
        if (Date.now()-last>25000) candidates.push({id:item.preview_id,key,last});
      }
    }
    const candidate=candidates.sort((a,b)=>a.last-b.last)[0];
    if (!candidate) return;
    const {id,key}=candidate;
    const info=await api(`${path(sessionId)}/plans/${encodeURIComponent(id)}/steps/${encodeURIComponent(key)}/info`);
    if (sessionId===getSession()) rememberCount(id,key,info.ionic_step);
  }

  let polling = false;
  async function refreshLive() {
    const id = getSession();
    if (!enabled || !id || pending || polling || document.hidden) return;
    polling = true;
    void refreshProgress?.(id);
    try {
      const data = await api(`${path(id)}/events`);
      if (id !== getSession() || !records) return;
      if (data.latest_report) await syncReport(id,data.latest_report);
      if (id !== getSession() || !records) return;
      const graphData=await api(`${path(id)}/workflows`);
      if(id!==getSession() || !records) return;
      const graphChanged=JSON.stringify(records.plans)!==JSON.stringify(graphData.plans)
        || JSON.stringify(records.task_agent_presets)!==JSON.stringify(graphData.task_agent_presets);
      records.plans=graphData.plans;
      records.task_agent_presets=graphData.task_agent_presets;
      onPlans?.(id,records.plans || []);
      const changes = Object.entries(data.jobs).some(([id, job]) => {
        const previous = records.live_jobs?.[id];
        return !previous || progressSignature(previous) !== progressSignature(job) || renderTaskStatus(previous, records.recovery?.[id]) !== renderTaskStatus(job, data.recovery?.[id]) || Boolean(previous.output_structure) !== Boolean(job.output_structure);
      });
      records.live_jobs = data.jobs; records.recovery = data.recovery;
      if (changes || graphChanged) render();
      await refreshVisibleCount(id);
      if (id !== getSession() || !records) return;
      const known = new Set(Object.values(records.submissions).map((value) => value.job.id));
      const previewIds = new Set(records.previews.map((value) => value.preview_id));
      if (Object.keys(data.jobs).some((id) => !known.has(id)) || Object.values(data.recovery || {}).some((value) => value?.result?.preview_id && !previewIds.has(value.result.preview_id))) await refresh();
    } catch { /* Notification panel shows stale-monitor warnings; do not spam toasts. */ }
    finally { polling = false; }
  }
  setInterval(refreshLive, 15000);

  return {
    refresh, drawStructures,
    async openAttention(item) {
      await refresh();
      const plan=records?.plans?.find(row=>row.id===item.plan);
      const node=plan?.nodes.find(row=>row.id===item.node_id);
      if(session!==item.session || !node) throw new Error('事项已更新，请查看当前对话。');
      const data=await api(path(session)+'/workflows/'+encodeURIComponent(plan.id)+'/attention');
      if(session!==item.session || getSession()!==session)return;
      const action=data.items.find(row=>(row.approval_id || row.job_id)===item.id);
      if(!action)throw new Error(t('事项已更新，请查看当前对话。'));
      selectedPlan=plan.id;render();
      attention={url:attentionUrl(),items:data.items,error:''};
      detail.openGeneral(plan,node,path(session),data.items,action);
    },
    drawReportFigures,
    enabled(value) {
      enabled = value;
      if (enabled) return;
      detail.close();
      selected = [];
      records = null;
      status.textContent = '暂无计算方案';
      onPlans?.(getSession(),[]);
      drawAttachments();
      render();
    },
    selection() { return [...selected]; },
    selectedIds() { return selected.map((item) => item.id); },
    clearSelection() { selected = []; drawAttachments(); },
    reset(nextSession = null) {
      knownSteps.clear(); detail.close();
      captureMaps(panel,{suspend:true,dispose:true}); panel.replaceChildren();
      selected = []; records = null; session = null;
      status.textContent = nextSession ? '正在读取计算方案…' : '暂无计算方案';
      onPlans?.(getSession(),[]); drawAttachments(); render();
    },
    busy() { return pending; },
  };
}
