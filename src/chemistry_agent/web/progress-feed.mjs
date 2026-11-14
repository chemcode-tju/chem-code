import {progressMarkdown} from "./answer-ui.mjs";
import {escapeHtml as esc} from './core.mjs';
import {workflowView} from './plan-presentation.mjs';

const kinds={submission:'提交回执',step:'步骤',diagnosis:'诊断',repair:'修复',interpretation:'Agent 解读',plan:'方案总结'};
const statusLabels={failed:'失败',blocked:'上游受阻',unconverged:'未收敛',needs_review:'待核对',submission_rejected:'提交被拒绝',paused:'已暂停',stopped:'已停止',cancelled:'已取消',running:'运行中',queued:'排队中',pending:'等待依赖',preparing:'准备中',submitted:'已提交',completed:'已完成',finished:'已结束',success:'已完成'};
export function progressStatus(status) {
  const group=['failed','blocked','unconverged','needs_review','submission_rejected','paused'].includes(status)?'attention'
    :['running','queued','pending','preparing','submitted'].includes(status)?'active'
    :['completed','finished','success','stopped','cancelled'].includes(status)?'complete':'other';
  return {group,label:statusLabels[status] || status || '记录'};
}
export function latestStepProgress(rows) {
  const latest=new Map();
  for(const row of rows) {
    if((row.kind!=='step' && !(row.kind==='repair' && row.status==='pending')) || !row.node_id)continue;
    const key=JSON.stringify([row.plan_id,row.node_id]);
    if(!latest.has(key) || Number(row.seq)>=Number(latest.get(key).seq))latest.set(key,row);
  }
  return [...latest.values()].sort((a,b)=>Number(b.seq)-Number(a.seq));
}
export function currentStepProgress(rows,plans=[]) {
  const latest=latestStepProgress(rows), indexed=new Map(latest.map(row=>[JSON.stringify([row.plan_id,row.node_id]),row]));
  const authoritative=new Set(), current=[];
  for(const plan of plans) {
    if(!Array.isArray(plan.nodes) || !plan.runtime)continue;
    authoritative.add(plan.id);
    for(const node of workflowView(plan).nodes) {
      const state=plan.runtime[node.id], previous=indexed.get(JSON.stringify([plan.id,node.id]));
      if(!state?.status) {if(previous)current.push(previous);continue;}
      let status=state.status;
      if(status==='running' && state.job) {
        if(state.job.paused)status='paused';
        else if(['queued','pending','waiting'].includes(state.job.scheduler_state))status='queued';
      }
      current.push({plan_id:plan.id,node_id:node.id,kind:'step',status,message:node.title,
        created_at:state.updated_at || state.completed_at || plan.updated_at || previous?.created_at || plan.created_at,
        detail:{node_kind:node.kind,scheduler_job_id:state.job?.scheduler_job_id}});
    }
  }
  // Runtime describes today's research steps; failed child executions remain in history.
  return [...current,...latest.filter(row=>!authoritative.has(row.plan_id))];
}
export function createProgressFeed({api,getSession,markdown,onFinal}) {
  const list=document.getElementById('progress-events');
  let session=null, cursor=0, loading=false, generation=0, rows=[], history=[], plans=[], planSignature='';
  const finals=new Set();
  let visibleRows=40;
  const folds=new Map();
  const label=row=>row.kind==='step' && ['model','analysis'].includes(row.detail?.node_kind)
    ? '辅助操作' : kinds[row.kind] || row.kind;
  list.addEventListener('click', event => {
    if(event.target.closest('[data-earlier]')) {visibleRows+=40;draw();}
  });
  const step=document.getElementById('step-screen');
  list.addEventListener('toggle',event=>{
    if(event.target.dataset.progressGroup)folds.set(event.target.dataset.progressGroup,event.target.open);
  },true);
  step.addEventListener('stepchange',draw);
  function draw() {
    const titles=new Map();
    for(const row of rows) if(row.kind==='step') titles.set(JSON.stringify([row.plan_id,row.node_id]),row.detail?.label || row.message);
    const item=row=>{
      const title=titles.get(JSON.stringify([row.plan_id,row.node_id]));
      const state=progressStatus(row.status);
      const job=row.detail?.scheduler_job_id ? ' · #'+esc(row.detail.scheduler_job_id) : '';
      const date=new Date(row.created_at),locale=document.documentElement.lang==='en'?'en-US':'zh-CN';
      const time=Number.isFinite(date.getTime())?`<time datetime="${esc(row.created_at)}" title="${esc(date.toLocaleString(locale))}">${esc(date.toLocaleString(locale,{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}))}</time>`:'';
      return `<article class="progress-event" data-state="${state.group}" data-status="${esc(row.status || '')}"><header><span class="progress-state">${esc(state.label)}</span><span>${esc(label(row))}${job}</span>${time}</header>${title && row.kind!=='step'?`<h4>${esc(title)}</h4>`:''}${progressMarkdown(markdown,row.message || '')}</article>`;
    };
    const updates=rows.filter(row=>row.kind!=='plan').slice().reverse();
    const items=updates.slice(0,visibleRows).map(item);
    if(updates.length>visibleRows) items.push('<button class="view-result" data-earlier>查看更早的进度</button>');
    const related=rows.filter(row=>row.plan_id===step.dataset.planId && row.node_id===step.dataset.nodeId);
    document.getElementById('step-replies').hidden=!related.length;
    document.getElementById('step-reply-list').innerHTML=related.map(item).join('');
    const messages=new Set(rows.map(row=>row.message));
    for(const turn of history) if(!finals.has(turn.event) && !turn.event.startsWith('plan:') && !messages.has(turn.answer)) {
      items.push(`<article><small>Agent · 历史步骤回复</small>${progressMarkdown(markdown,turn.answer)}</article>`);
    }
    const latest=currentStepProgress(rows,plans);
    const fold=(key,title,body,count,open)=>`<details class="progress-group" data-progress-group="${key}"${(folds.get(key) ?? open)?' open':''}><summary><span>${title}</span><span class="progress-count">${count}</span></summary>${body}</details>`;
    const groups=[['attention','需处理',true],['active','进行与等待',true],['complete','已结束',false],['other','其他状态',false]].map(([key,title,open])=>{
      const records=latest.filter(row=>progressStatus(row.status).group===key);
      return records.length?fold(key,title,records.map(item).join(''),records.length,open):'';
    }).join('');
    const historyMarkup=items.length?fold('history','更新记录',items.join(''),updates.length,false):'';
    list.innerHTML=groups ? groups+historyMarkup : items.join('') || '<p class="inspector-empty">暂无步骤回复</p>';
  }
  function reset(id=null) {
    session=id; generation++; cursor=0; visibleRows=40; rows=[]; history=[]; plans=[];planSignature='';finals.clear();folds.clear();loading=false;draw();
  }
  async function refresh(id) {
    if(id!==getSession())return;
    if(session!==id) reset(id);
    if(!id || loading) return;
    loading=true;
    const token=generation;
    try {
      let more, changed=false;
      do {
        const data=await api(`/api/automation/chat/${encodeURIComponent(id)}/progress?after=${cursor}&limit=100`);
        if(token!==generation || id!==getSession()) return;
        const fresh=data.events.filter(row=>row.seq>cursor);
        rows.push(...fresh);
        const previous=cursor;
        cursor=data.next_cursor ?? cursor;
        more=data.has_more && cursor>previous;
        changed ||= fresh.length>0;
      } while(more);
      if(changed) draw();
      for(const row of rows) {
        const key=row.detail?.chat_event;
        if(row.kind==='plan' && row.node_id==null && key && !finals.has(key)) {
          finals.add(key);
          try {if(!await onFinal(id,key) && generation===token) finals.delete(key);} catch(error) {if(generation===token) finals.delete(key);throw error;}
          if(generation!==token) return;
          draw();
        }
      }
    } catch { /* Retry on the next refresh; older servers can display history. */ }
    finally {if(token===generation) loading=false;}
  }
  return {refresh,reset,isFinal:key=>key.startsWith('plan:') || finals.has(key),setPlans(id,value) {
    if(id!==getSession())return;
    if(session!==id)reset(id);
    const next=Array.isArray(value)?value:[];
    const signature=JSON.stringify([next.filter(plan=>Array.isArray(plan.nodes)&&plan.runtime).map(plan=>plan.id),currentStepProgress([],next)]);
    plans=next;
    if(signature!==planSignature){planSignature=signature;draw();}
  },setHistory(id,turns) {
    if(id!==getSession())return;
    if(session!==id) reset(id);
    const next=turns.filter(turn=>turn.event);
    if(next.length!==history.length || next.some((turn,i)=>turn.answer!==history[i]?.answer)) {history=next;draw();}
  }};
}
