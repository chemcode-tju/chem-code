import {planMap} from './plan-map.mjs';
import {escapeHtml as esc} from './core.mjs';

export const planStatus = status => ({draft:'待确认',running:'执行中',paused:'已暂停推进',completed:'已完成',failed:'有步骤失败',pending:'等待依赖',preparing:'准备中',blocked:'上游失败，未执行'})[status] || status;
export function nodeStatus(plan,node) {
  const state=plan.runtime[node.id], job=state.job;
  if(plan.status==='draft') return '待确认';
  if(state.status==='pending' && node.depends_on.every(id=>plan.runtime[id].status==='completed')) return '等待执行';
  if(state.status==='running' && job) {
    if(state.quota_move && !job.scheduler_job_id) return '正在切换至 '+state.quota_move.target;
    if(state.quota_wait_since && !job.scheduler_job_id) return '等待队列名额';
    if(job.paused) return 'AiiDA 已暂停';
    if(job.scheduler_state==='running') return '运行中';
    if(job.scheduler_job_id) return '已交调度器';
    return '正在提交';
  }
  return planStatus(state.status);
}
export function graphLayers(nodes) {
  const pending=new Map(nodes.map(n=>[n.id,n])), seen=new Set(), layers=[];
  while(pending.size) {
    const ready=[...pending.values()].filter(n=>n.depends_on.every(id=>seen.has(id)));
    if(!ready.length) throw new Error('方案依赖有环或缺少节点');
    layers.push(ready);
    for(const node of ready) {pending.delete(node.id);seen.add(node.id);}
  }
  return layers;
}
export function renderGeneralPlan(plan) {
  const titles=Object.fromEntries(plan.nodes.map(n=>[n.id,n.title]));
  const compute = plan.nodes.filter(node => node.kind==='vasp');
  const completed = compute.filter(node=>plan.runtime[node.id].status==='completed').length;
  const layers = graphLayers(plan.nodes).map(layer => layer.map(node => ({
    id:node.id, title:node.title, dependencies:node.depends_on, auxiliary:node.kind!=='vasp',
    description:(layer.length>1?'独立分支 · 可并行。':'')+(node.depends_on.length?'依赖：'+node.depends_on.map(id=>titles[id]).join('、'):node.title),
    state:plan.runtime[node.id].status, status:(plan.runtime[node.id].job?.scheduler_job_id?'#'+plan.runtime[node.id].job.scheduler_job_id+' · ':'')+(plan.status==='draft' && plan.revision>1 && plan.changed_nodes.includes(node.id)?'已修改 · ':'')+nodeStatus(plan,node)
  })));
  const graph = planMap(layers,plan.id,'data-general-step');
  return `<article class="calculation-preview general-plan" data-task="${esc(plan.id)}" data-general-plan="${esc(plan.id)}"><header><strong>${esc(plan.title)}</strong><small>v${plan.revision}</small></header><p class="task-parameters">${esc(planStatus(plan.status))} · ${completed}/${compute.length} 项计算 · ${plan.max_parallel ? `最多 ${esc(plan.max_parallel)} 个计算并行` : '就绪即提交'}</p><div class="graph-progress" role="progressbar" aria-label="方案完成步骤" aria-valuemin="0" aria-valuemax="${compute.length}" aria-valuenow="${completed}"><span style="width:${100*completed/Math.max(1,compute.length)}%"></span></div>${graph}<details data-fold="goal"><summary>研究目标与版本</summary><p>${esc(plan.goal)}</p>${plan.history.map(h=>`<p>v${h.revision} · 修改 ${h.changed_nodes.length} 步${h.removed_nodes.length?` · 移除 ${h.removed_nodes.map(esc).join('、')}`:''}</p>`).join('')}</details>${plan.status==='draft'?`<div class="task-actions"><button data-revise-plan="${esc(plan.id)}">提出修改</button><button class="tool-submit" data-confirm-plan="${esc(plan.id)}">确认方案并执行</button></div>`:['running','paused'].includes(plan.status)?`<button data-control-plan="${esc(plan.id)}" data-plan-action="${plan.status==='running'?'pause':'resume'}">${plan.status==='running'?'暂停后续步骤':'继续执行'}</button>`:''}</article>`;
}
export function generalRows(plan,node) {
  const params=node.effective, names={model:'ASE 建模',vasp:'VASP 计算',analysis:'Python 分析'};
  const rows=[['操作',names[node.kind]],['目的',node.purpose],['输入',Object.entries(node.inputs).map(([alias,ref])=>`${alias} ← ${ref.node?`${plan.nodes.find(n=>n.id===ref.node)?.title} / ${ref.output==='structure'?'实际输出结构':'计算结果'}`:'已保存结构'}`).join('\n')]];
  if(params) rows.push(['INCAR',Object.entries(params.incar).map(([k,v])=>`${k} = ${v}`).join('\n')],['k 点',params.kpoint_spacing?`间距 ${params.kpoint_spacing} Å⁻¹（依实际晶胞生成）`:params.kmesh.join(' × ')],['资源',`${params.resources.queue} · ${params.resources.nodes} 节点 · ${params.resources.wallclock_seconds/3600} h`],['完成条件',[node.calculation.require_electronic?'电子收敛':null,node.calculation.require_ionic?'离子收敛':null,'正常退出'].filter(Boolean).join('、')]);
  return rows.filter(([,v])=>v).map(([k,v])=>`<dt>${esc(k)}</dt><dd style="white-space:pre-wrap">${esc(v)}</dd>`).join('');
}
