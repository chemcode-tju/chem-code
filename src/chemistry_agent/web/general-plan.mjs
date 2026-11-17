import {planMap} from './plan-map.mjs';
import {workflowView, childJobSummary} from './plan-presentation.mjs';
import {taskLabel} from './plan-layout.mjs';
import {escapeHtml as esc} from './core.mjs';

export const planStatus = status => ({draft:'待确认',running:'执行中',paused:'已暂停推进',stopping:'停止中',stopped:'已停止',completed:'已完成',failed:'有步骤失败',pending:'等待依赖',preparing:'准备中',blocked:'上游失败，未执行'})[status] || status;
export function nodeStatus(plan,node) {
  const state=plan.runtime[node.id], job=state.job;
  if(plan.status==='draft') return '待确认';
  if(state.status==='failed') return '步骤失败';
  if(state.status==='pending' && node.depends_on.every(id=>plan.runtime[id].status==='completed')) return '等待执行';
  if(state.status==='running' && job) {
    if(state.quota_move && !job.scheduler_job_id) return '正在切换至 '+state.quota_move.target;
    if(state.quota_wait_since && !job.scheduler_job_id) return '等待队列名额';
    if(job.backend==='slurm' && job.status==='quota_wait') return '等待队列名额';
    if(job.backend==='slurm' && job.status==='uncertain') return '正在核对提交结果';
    if(job.backend==='slurm' && job.scheduler_state==='unknown') return '等待调度记账结果';
    if(job.paused) return 'AiiDA 已暂停';
    if(job.scheduler_state==='running') return '运行中';
    if(job.scheduler_state==='pending' || job.scheduler_state==='queued') return '排队中';
    if(job.scheduler_job_id) return '已交调度器';
    return '正在提交';
  }
  return planStatus(state.status);
}
export function graphLayers(nodes) {
  const index=new Map(nodes.map((n,i)=>[n.id,i])), next=nodes.map(()=>[]), counts=nodes.map(n=>n.depends_on.length);
  const invalid=()=>new Error('方案依赖有环或缺少节点');
  if(index.size!==nodes.length) throw invalid();
  nodes.forEach((n,i)=>n.depends_on.forEach(id=>{
    if(!index.has(id)) throw invalid();
    next[index.get(id)].push(i);
  }));
  const layers=[];
  let ready=nodes.map((_,i)=>i).filter(i=>counts[i]===0), visited=0;
  while(ready.length) {
    layers.push(ready.map(i=>nodes[i]));visited+=ready.length;
    const following=[];
    for(const i of ready) for(const child of next[i]) if(--counts[child]===0) following.push(child);
    ready=following.sort((a,b)=>a-b);
  }
  if(visited!==nodes.length) throw invalid();
  return layers;
}
export function renderGeneralPlan(plan) {
  const view = workflowView(plan);
  const titles=Object.fromEntries(plan.nodes.map(n=>[n.id,n.title]));
  const compute = view.nodes.filter(node => ['vasp','job','task'].includes(node.kind));
  const completed = compute.filter(node=>plan.runtime[node.id].status==='completed').length;
  const layers = graphLayers(view.nodes).map(layer => layer.map(node => ({
    id:node.id, title:node.title, groupPath:node.group_path, dependencies:node.depends_on, auxiliary:!['vasp','job','task'].includes(node.kind),
    kind:node.kind, jobSummary:childJobSummary(plan,view.children.get(node.id)),
    searchText:(view.children.get(node.id) || []).map(child=>[child.id,child.title,plan.runtime[child.id]?.job?.scheduler_job_id].filter(Boolean).join(' ')).join(' '),
    description:node.depends_on.length?'依赖：'+node.depends_on.map(id=>titles[id]).join('、'):'',
    boardState:plan.status==='draft'?'draft':plan.runtime[node.id].status==='running' && plan.runtime[node.id].job?.paused?'paused':plan.runtime[node.id].status,
    state:plan.runtime[node.id].status, status:(plan.status==='draft' && plan.revision>1 && plan.changed_nodes.includes(node.id)?'已修改 · ':'')+nodeStatus(plan,node)
  })));
  const controls = plan.status==='draft'
    ? `<button type="button" data-revise-plan="${esc(plan.id)}" title="提出修改" aria-label="提出修改"><svg class="icon" aria-hidden="true"><use href="/assets/ui-icons.svg#i-pencil"/></svg></button><button type="button" class="tool-submit" data-confirm-plan="${esc(plan.id)}" title="确认并执行" aria-label="确认并执行"><svg class="icon" aria-hidden="true"><use href="/assets/ui-icons.svg#i-check"/></svg><span>确认并执行</span></button>`
    : ['running','paused'].includes(plan.status)
      ? `<button type="button" data-control-plan="${esc(plan.id)}" data-plan-action="${plan.status==='running'?'pause':'resume'}" title="${plan.status==='running'?'暂停后续步骤':'继续执行'}" aria-label="${plan.status==='running'?'暂停后续步骤':'继续执行'}"><svg class="icon" aria-hidden="true"><use href="/assets/ui-icons.svg#i-${plan.status==='running'?'pause':'play'}"/></svg></button>` : '';
  const graph = planMap(layers,plan.id,'data-general-step',plan.title,`<div class="plan-run-controls" title="${esc(planStatus(plan.status))} · ${completed}/${compute.length} 项计算">${controls}</div>`);
  return `<article class="calculation-preview general-plan plan-surface" data-task="${esc(plan.id)}" data-general-plan="${esc(plan.id)}">${graph}</article>`;
}
export function generalRows(plan,node) {
  const params=node.effective, byId=new Map(plan.nodes.map(n=>[n.id,n]));
  const rows=[['输入',Object.entries(node.inputs || {}).map(([alias,ref])=>`${alias} ← ${ref.node?`${byId.has(ref.node)?taskLabel(byId.get(ref.node)):ref.node} / ${ref.output==='structure'?'实际输出结构':'计算结果'}`:'已保存结构'}`).join('\n')]];
  if(params) rows.push(['k 点',params.kpoint_spacing?`间距 ${params.kpoint_spacing} Å⁻¹`:params.kmesh?.join(' × ')],['资源',`${params.resources.queue} · ${params.resources.nodes} 节点 · ${params.resources.wallclock_seconds/3600} h`],['完成条件',[node.calculation.require_electronic?'电子收敛':null,node.calculation.require_ionic?'离子收敛':null,'正常退出'].filter(Boolean).join('、')]);
  if(node.job) {
    const runtime=plan.runtime[node.id]?.job || {}, resources=node.job.resources;
    rows.push(['队列',resources?.queue || runtime.queue],['服务器',node.job.host],
      ['运行时限',resources?.wallclock_seconds ? `${resources.wallclock_seconds} s` : null],
      ['来源',node.job.adopted?'接管已有作业':null],
      ['工作目录',node.job.adopted?runtime.remote_directory:null],
      ['取回文件',node.job.retrieve.join('、')],['完成条件','调度结束、退出码为 0、要求的输出文件存在']);
  }
  if(node.kind==='task') rows.push(['执行负责人','Agent'],['关联作业',(plan.runtime[node.id]?.jobs || []).map(id=>Object.values(plan.runtime).find(s=>s.generic_job_id===id)?.job?.scheduler_job_id || id).join('、')]);
  if(node.files?.length) rows.push(['文件依赖',node.files.map(f=>`${f.node || f.job_id || f.file_id || f.artifact_id}: ${f.path || '附件'} → ${f.target}`).join('\n')]);
  const entries=Object.entries(params?.incar || {});
  const incar=entries.length ? `<dt>INCAR</dt><dd><details class="param-list"><summary>${entries.length} 项参数</summary><div>${entries.map(([k,v])=>`<code><b>${esc(k)}</b> = ${esc(String(v))}</code>`).join('')}</div></details></dd>`:'';
  return rows.filter(([,v])=>v).map(([k,v])=>`<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('')+incar;
}
