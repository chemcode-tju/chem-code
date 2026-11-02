import {escapeHtml as esc} from './core.mjs';

const kinds={submission:'提交回执',step:'步骤',diagnosis:'诊断',repair:'修复',interpretation:'Agent 解读',plan:'方案总结'};
export function createProgressFeed({api,getSession,markdown,onFinal}) {
  const list=document.getElementById('progress-events');
  let session=null, cursor=0, loading=false, generation=0, rows=[], history=[];
  const finals=new Set();
  let visibleRows=40, inboxRows=16;
  const inbox=document.getElementById('progress-inbox');
  const label=row=>row.kind==='step' && ['model','analysis'].includes(row.detail?.node_kind)
    ? '辅助操作' : kinds[row.kind] || row.kind;
  list.addEventListener('click', event => {
    if(event.target.closest('[data-earlier]')) {visibleRows+=40;draw();}
  });
  inbox.addEventListener('click', event => {
    if(event.target.closest('[data-earlier]')) {inboxRows+=16;draw();}
  });
  const step=document.getElementById('step-screen');
  step.addEventListener('stepchange',draw);
  function draw() {
    const titles=new Map();
    for(const row of rows) if(row.kind==='step') titles.set(JSON.stringify([row.plan_id,row.node_id]),row.detail?.label || row.message);
    const item=row=>{
      const title=titles.get(JSON.stringify([row.plan_id,row.node_id]));
      const state=row.status ? ' · '+esc(row.status) : '';
      const job=row.detail?.scheduler_job_id ? ' · #'+esc(row.detail.scheduler_job_id) : '';
      return `<article><small>${esc(label(row))}${state}${job} · ${esc(new Date(row.created_at).toLocaleString())}</small>${title && row.kind!=='step'?`<h4>${esc(title)}</h4>`:''}<div class="markdown">${markdown.render(row.message || '')}</div></article>`;
    };
    const updates=rows.filter(row=>row.kind!=='plan').slice().reverse();
    const items=updates.slice(0,visibleRows).map(item);
    if(updates.length>visibleRows) items.push('<button class="view-result" data-earlier>查看更早的进度</button>');
    const related=rows.filter(row=>row.plan_id===step.dataset.planId && row.node_id===step.dataset.nodeId);
    document.getElementById('step-replies').hidden=!related.length;
    document.getElementById('step-reply-list').innerHTML=related.map(item).join('');
    const seen=new Set();
    const briefs=rows.slice().reverse().filter(row=>{
      const key=row.detail?.run_id || row.seq;
      if(seen.has(key)) return false;
      seen.add(key);return true;
    });
    const open=new Set([...inbox.querySelectorAll('details[open]')].map(el=>el.dataset.event));
    inbox.innerHTML=briefs.length?'<h3>当前会话</h3>'+briefs.slice(0,inboxRows).map(row=>
      `<details data-event="${esc(row.seq)}" ${open.has(String(row.seq))?'open':''}><summary>${esc(label(row))} · ${esc(row.message.split('\n')[0].slice(0,120))}</summary><div class="markdown">${markdown.render(row.message)}</div></details>`
    ).join('')+(briefs.length>inboxRows?'<button class="view-result" data-earlier>查看更早的消息</button>':''):'';
    const messages=new Set(rows.map(row=>row.message));
    for(const turn of history) if(!finals.has(turn.event) && !turn.event.startsWith('plan:') && !messages.has(turn.answer)) {
      items.push(`<article><small>Agent · 历史步骤回复</small><div class="markdown">${markdown.render(turn.answer)}</div></article>`);
    }
    list.innerHTML=items.join('') || '<p class="inspector-empty">暂无步骤回复</p>';
  }
  function reset(id=null) {
    session=id; generation++; cursor=0; visibleRows=40; inboxRows=16; rows=[]; history=[]; finals.clear();loading=false;draw();
  }
  async function refresh(id) {
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
  return {refresh,reset,isFinal:key=>key.startsWith('plan:') || finals.has(key),setHistory(id,turns) {
    if(session!==id) reset(id);
    const next=turns.filter(turn=>turn.event);
    if(next.length!==history.length || next.some((turn,i)=>turn.answer!==history[i]?.answer)) {history=next;draw();}
  }};
}
