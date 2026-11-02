import {planMap} from './plan-map.mjs';
import {escapeHtml as esc} from './core.mjs';

function stepState(step, job, child) {
  if (step.reused_pk) return ['done','已复用'];
  if (child) {
    if (child.branch_valid === false && child.finished_ok) return ['failed','优化检查未通过'];
    if (child.finished_ok) {
      const status = child.results?.run_status;
      if (status?.electronic_converged === false || (status?.nsw > 0 && status.ionic_converged === false)) return ['failed','未收敛'];
      return ['done','已结束'];
    }
    if (child.paused || ['finished','excepted','killed'].includes(child.process_state)) return ['failed',child.paused?'已暂停':'失败'];
    if (child.scheduler_state === 'running') return ['active','运行中'];
    return ['active',child.scheduler_job_id?'已交调度器':'准备中'];
  }
  if (step.stage === 'energies' && job?.adsorption) return ['done','已汇总'];
  if (job && ['finished','excepted','killed'].includes(job.process_state)) return ['idle','未执行'];
  return ['idle',job?'等待前序':'待确认'];
}

export function planSteps(item) {
  return item.stages?.length ? item.stages : [{stage:'single', title:'计算',incar:item.request?.incar,kmesh:item.request?.kmesh}];
}

export function describeStep(item,job,step) {
      const key=step.stage, child=key==='single'?job:job?.steps?.find(s=>s.label?.split(':').at(-1)===key);
      const [tone,status]=stepState(step,job,child);
      const title = step.title || ({relax:'结构优化',singlepoint:'单点能量'})[key] || key;
      const mesh=step.kmesh || (step.incar && !step.reused_pk && item.request?.kmesh);
      const rows = Object.entries(step.incar || {}).map(([k,v])=>[k.toUpperCase(),Array.isArray(v)?v.join(' '):String(v)]);
      if (mesh) rows.push(['k 网格',mesh.join(' × ')]);
      if (step.fixed_atoms != null) rows.push(['固定原子',step.fixed_atoms]);
      if (key==='surface' && item.surface_plan) rows.push(['表面',`fcc(100) · ${item.surface_plan.size?.join('×') || ''}`],['层数',item.surface_plan.layers],['固定层',item.surface_plan.fixed_layers],['真空',item.surface_plan.vacuum_angstrom+' Å']);
      if (key==='gas') rows.push(['盒长',item.surface_plan.gas_cell+' Å']);
      if (['top','bridge','hollow'].includes(key)) rows.push(['初始 C 高度',item.surface_plan.co_height+' Å'],['C–O 键长',item.surface_plan.co_bond+' Å']);
      if (step.formula) rows.push(['定义',step.formula]);
      if (key==='energies') for(const [site,e] of Object.entries(job?.adsorption?.adsorption_ev||{})) rows.push([site,e.toFixed(4)+' eV']);
      if (child?.pk || step.reused_pk) rows.push(['AiiDA','#'+(child?.pk||step.reused_pk)]);
      if (child?.scheduler_job_id) rows.push(['作业号',child.scheduler_job_id]);
  if (step.resources?.wallclock_seconds) rows.push(['时限',step.resources.wallclock_seconds+' 秒']);
  return {key,child,title,status,tone,rows,nsw:step.incar?.NSW ?? step.incar?.nsw};
}

export function stepRows(rows) {
  return rows.map(([key,value])=>`<dt>${esc(key)}</dt><dd>${esc(String(value))}</dd>`).join('');
}

export function workflowPlan(item,job,sessionPath='') {
  const stages = planSteps(item);
  const groups = [];
  for (const step of stages) {
    if (step.parallel && groups.at(-1)?.[0].parallel) groups.at(-1).push(step);
    else groups.push([step]);
  }
  if (sessionPath) {
    const layers = groups.map((group,index)=>group.map(step=>{
      const detail=describeStep(item,job,step);
      return {id:detail.key,title:detail.title,status:detail.status,state:detail.tone,
        auxiliary:step.stage==='energies',dependencies:index?groups[index-1].map(parent=>parent.stage):[]};
    }));
    return planMap(layers,item.preview_id,'data-open-step');
  }
  const active = groups.findIndex(group => group.some(step => {
    const child = job?.steps?.find(s=>s.label?.split(':').at(-1)===step.stage);
    return stepState(step,job,child)[0]==='active';
  }));
  const heading = active<0 ? `共 ${groups.length} 步` : `进行到第 ${active+1} / ${groups.length} 步`;
  return `<div class="workflow-plan" aria-label="计算流程"><div class="plan-heading"><strong>执行方案</strong><small>${heading}</small></div>`+groups.map((steps,i)=>{
    const parallel = steps[0].parallel;
    return '<div class="plan-group"'+(active===i?' aria-current="step"':'')+'><span class="plan-number">'+(i+1)+'</span><div class="plan-nodes">'+(parallel?'<small>并行 · 独立提交</small>':'')+steps.map(step=>{
      const {key,child,title,status,tone,rows} = describeStep(item,job,step);
      if (sessionPath) {
        const count = child?.results?.run_status?.last_iteration_index?.[0];
        return `<button class="plan-entry" type="button" data-open-step="${esc(key)}" data-state="${tone}"><span>${esc(title)}</span><small>${esc(status)}<span data-ionic-step>${count ? ' · '+esc(String(count))+' 步' : ''}</span></small><span aria-hidden="true">›</span></button>`;
      }
      return `<details data-fold="step-${esc(key)}" class="plan-step" data-state="${tone}"><summary><span>${esc(title)}</span><small>${status}</small></summary><dl>${rows.map(([k,v])=>`<dt>${esc(k)}</dt><dd>${esc(String(v))}</dd>`).join('')}</dl>${!job?`<button type="button" data-revise-step="${esc(title)}">提出修改</button>`:''}</details>`;
    }).join('')+'</div></div>';
  }).join('')+'</div>';
}
