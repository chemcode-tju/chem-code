import {escapeHtml as esc} from './core.mjs';
import {taskLabel} from './plan-layout.mjs';

const columns = [
  ['todo','待执行'], ['active','进行中'], ['attention','需处理'], ['done','已完成'],
  ['ended','已停止'], ['other','其他状态'],
];

// Group research-step runtime, never the outcome of an old child execution.
export function kanbanState(state) {
  if (['draft','pending','idle'].includes(state)) return 'todo';
  if (['running','active','preparing','submitted','queued','stopping'].includes(state)) return 'active';
  if (['failed','blocked','paused','unconverged','needs_review'].includes(state)) return 'attention';
  if (['completed','done'].includes(state)) return 'done';
  if (['cancelled','stopped','killed'].includes(state)) return 'ended';
  return 'other';
}

export function planKanban(nodes, attribute) {
  return `<div class="plan-kanban" role="region" aria-label="方案看板" tabindex="0" hidden>${columns.map(([key,label])=>{
    const members=nodes.filter(node=>kanbanState(node.boardState ?? node.state)===key);
    return `<section class="kanban-column" data-column="${key}" aria-label="${label}" ${!members.length && ['ended','other'].includes(key)?'hidden':''}>
      <h3><i aria-hidden="true"></i>${label}<span data-column-count>${members.length}</span></h3>
      <ul>${members.map(node=>{
        const title=taskLabel(node), group=node.groupPath?.join(' / ');
        return `<li data-board-node="${esc(node.id)}"><button type="button" class="kanban-card" ${attribute}="${esc(node.id)}" title="${esc([title,group,node.status].filter(Boolean).join(' · '))}">
          <strong>${esc(title)}</strong>${group?`<span class="kanban-group">${esc(group)}</span>`:''}
          <span class="kanban-status">${esc(node.status)}</span></button></li>`;
      }).join('')}</ul><p class="kanban-empty" ${members.length?'hidden':''}>暂无步骤</p></section>`;
  }).join('')}</div>`;
}
