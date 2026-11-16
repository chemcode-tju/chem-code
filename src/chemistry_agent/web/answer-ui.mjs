import {escapeHtml as esc, TOOL_LABELS} from './core.mjs';
import {getLanguage,t} from './i18n.mjs';

const icon = name => `<svg class="icon" aria-hidden="true"><use href="/assets/ui-icons.svg#i-${name}"/></svg>`;
const button = (attribute, label) => `<button type="button" class="copy-action" ${attribute} aria-label="${t(label)}" title="${t(label)}">${icon('copy')}</button>`;

export function duration(start,end) {
  const delta = Date.parse(end) - Date.parse(start);
  if (!Number.isFinite(delta) || delta < 0) return '';
  const seconds = Math.floor(delta / 1000), minutes = Math.floor(seconds / 60);
  return getLanguage()==='en' ? (minutes ? minutes+'m ' : '')+(seconds%60)+'s'
    : (minutes ? minutes+' 分 ' : '')+(seconds%60)+' 秒';
}

export function activitySteps(steps=[], calls=[], {deferDetails=false} = {}) {
  const byId = new Map(calls.map(call=>[call.call_id,call]));
  const rows = steps.length ? steps : calls.map(call=>({type:'tool',...call,status:call.success===false?'failed':'completed'}));
  return rows.map((step,index)=>{
    if (step.type === 'commentary') return `<div class="activity-commentary" data-no-i18n>${esc(step.text)}</div>`;
    const call = step.call_id ? byId.get(step.call_id) || step : step;
    const name = esc(t(TOOL_LABELS[step.name] || step.name || '工具'));
    const status = t(({running:'执行中',completed:'已完成',failed:'需调整输入',uncertain:'结果待核对'})[step.status] || '结果待核对');
    if (call.arguments===undefined && call.result===undefined) return `<div class="activity-item" data-status="${esc(step.status)}">${name}<small>${status}</small></div>`;
    const detail = deferDetails ? '' : activityDetail(steps,calls,index);
    return `<details class="activity-item"${deferDetails ? ` data-deferred-call="${index}"` : ''}><summary>${name}<small>${status}</small>${icon('chevron')}</summary>${detail}</details>`;
  }).join('');
}

export function activityDetail(steps, calls, index) {
  const step = (steps.length ? steps : calls)[index];
  if (!step) return '';
  const call = step.call_id ? calls.find(item=>item.call_id === step.call_id) || step : step;
  return `<pre>${esc(JSON.stringify({arguments:call.arguments,result:call.result},null,2))}</pre>`;
}

export function initDeferredActivities({root, resolve}) {
  root.addEventListener('toggle', event => {
    const target = event.target;
    if (!target.open || !root.contains(target)) return;
    const activity = target.closest('[data-activity]');
    if (!activity) return;
    const record = resolve(activity.dataset.activity);
    if (!record) return;
    const {steps=[], calls=[]} = record;
    if (target.hasAttribute('data-deferred-activity')) {
      target.removeAttribute('data-deferred-activity');
      const rows = activitySteps(steps,calls,{deferDetails:true});
      target.querySelector('[data-activity-steps]').innerHTML = rows;
      target.querySelector('[data-activity-placeholder]').hidden = Boolean(rows);
    } else if (target.hasAttribute('data-deferred-call')) {
      const index = Number(target.dataset.deferredCall);
      target.removeAttribute('data-deferred-call');
      target.insertAdjacentHTML('beforeend',activityDetail(steps,calls,index));
    }
  }, true);
}

export function activityOpen(running, previous) {
  return previous && previous.running === running ? previous.open : running;
}

export function activity({id,start,end,calls=[],steps=[],running=false,draftRequest=null,deferred=false}) {
  if (!start && !calls.length && !steps.length && !draftRequest) return '';
  const clock = duration(start,end || new Date().toISOString());
  const label = clock ? t(running?'已用时':'用时')+' '+clock : '';
  const rows = deferred ? "" : activitySteps(steps,calls);
  const draft = draftRequest ? `<div class="answer-output activity-draft" hidden>${liveCopyButton()}<div class="markdown" data-live-request="${esc(draftRequest)}"></div></div>` : '';
  return `<details class="turn-activity" data-activity="${esc(id)}" data-running="${running}"${deferred ? ' data-deferred-activity' : ''}${running?' open':''}><summary aria-label="${t('处理过程')}"><span${running && start?' data-clock="'+esc(start)+'"':''}>${esc(label)}</span>${icon('chevron')}</summary><div class="activity-log"><div data-activity-steps>${rows}</div><p data-activity-placeholder${rows?' hidden':''}>${t(running?'正在生成下一步…':'未调用工具')}</p>${draft}</div></details>`;
}

export function answerBox(html,index) {
  return `<div class="answer-output">${button('data-copy="'+index+'"','复制回答')}<div class="markdown">${html}</div></div>`;
}
export function progressMarkdown(markdown,source) {
  return `<div class="answer-output" data-source="${esc(source)}">${button("data-copy-markdown","复制回答")}<div class="markdown">${markdown.render(source)}</div></div>`;
}
export const liveCopyButton = () => button('data-copy-live','复制回答');

export function initAnswerControls({root, answer, toast}) {
  function equip(pre) {
    if (!pre.closest('.markdown,.activity-item,.calculation-preview,.model-record,.step-sections,.raw-result,.general-plan') || pre.parentElement?.classList.contains('copy-block')) return;
    const box=document.createElement('div');
    box.className='copy-block';
    pre.before(box);box.append(pre);box.insertAdjacentHTML('beforeend',button('data-copy-block','复制内容'));
  }
  const observer = new MutationObserver(records=>{
    for (const record of records) for (const node of record.addedNodes) {
      if (node.nodeType!==1 || (node.tagName!=='PRE' && !node.childElementCount)) continue;
      if (node.tagName==='PRE') equip(node);
      else node.querySelectorAll('pre').forEach(equip);
    }
  });
  observer.observe(root,{subtree:true,childList:true});
  root.querySelectorAll('pre').forEach(equip);
  root.addEventListener('click',async event=>{
    const control=event.target.closest('.copy-action');
    if (!control) return;
    const source=control.hasAttribute('data-copy-params') ? Array.from(control.parentElement.querySelectorAll('dt'),term=>term.textContent+' = '+term.nextElementSibling?.textContent).join('\n')
      : control.hasAttribute('data-copy-markdown') ? control.parentElement.dataset.source
      : control.hasAttribute('data-copy-block') ? control.parentElement.querySelector('pre').textContent
      : control.hasAttribute('data-copy-live') ? control.parentElement.querySelector('[data-live-request]')?.dataset.preview : answer(Number(control.dataset.copy));
    if (source==null) return;
    try {
      if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(source);
      else {
        const field=document.createElement('textarea');
        field.value=source;field.className='sr-only';
        (control.closest('dialog[open]') || root).append(field);field.select();
        const ok=document.execCommand('copy');field.remove();control.focus({preventScroll:true});
        if (!ok) throw new Error();
      }
      control.innerHTML=icon('check');control.setAttribute('aria-label',t('已复制'));
      setTimeout(()=>{control.innerHTML=icon('copy');control.setAttribute('aria-label',control.title);},1400);
    } catch { toast(t('复制不可用，请选中文字后手动复制。')); }
  });
  setInterval(()=>{for(const el of root.querySelectorAll('[data-clock]')) {
    el.textContent=t('已用时')+' '+duration(el.dataset.clock,new Date().toISOString());
  }},1000);
}
