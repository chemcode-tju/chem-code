import {escapeHtml as esc} from './core.mjs';

const states = {
  draft:'草稿',running:'运行中',completed:'已完成',failed:'失败',pending:'待执行',
  paused:'已暂停',stopped:'已停止',stopping:'停止中',blocked:'受阻',
  waiting:'等待中',matched:'已触发',expired:'已过期',cancelled:'已取消',
  retry_wait:'等待重试',uncertain:'需核对回执',bundled:'已合并',superseded:'已取代',
};
const label = value => esc(states[value] || value);
const chevron = '<svg class="icon disclosure" aria-hidden="true"><use href="/assets/ui-icons.svg#i-chevron"/></svg>';

function conditionText(condition) {
  if (condition.op === 'file') return `${condition.node} · ${condition.pattern}`;
  if (condition.op === 'state') return `${condition.node} · ${condition.states.join(' / ')}`;
  const separator = condition.op === 'all' ? ' ∧ ' : ' ∨ ';
  return '(' + condition.conditions.map(conditionText).join(separator) + ')';
}

export function drawWorkflows(container, {plans, unreadable}) {
  const html = plans.map(plan => {
    const rows = plan.nodes.map(node => `<li><span data-no-i18n>${esc(node.title)}</span>
      <span>${label(node.status)}</span>${node.scheduler_job_id ? `<small data-no-i18n>Slurm #${esc(node.scheduler_job_id)} ${esc(node.scheduler_state || '')}</small>` : ''}</li>`).join('');
    const waits = plan.waits.map(wait => `<li><div><strong data-no-i18n>${esc(wait.key)}</strong>
      <code>${esc(conditionText(wait.condition))}</code><span data-no-i18n>${esc(wait.instruction)}</span></div>
      <span>${label(wait.status)}</span>${wait.delivery ? `<small><span>唤醒投递</span> · <span>${label(wait.delivery)}</span></small>` : ''}</li>`).join('');
    const receipts = Object.entries(plan.deliveries).map(([state,count]) =>
      `<li><span>${label(state)}</span><span>${esc(count)}</span></li>`).join('');
    const id = esc(plan.session_id + '/' + plan.id);
    return `<details data-workflow="${id}" class="wake-defaults"><summary>
      <span class="wake-flow-title"><strong data-no-i18n>${esc(plan.title)}</strong><small data-no-i18n>${esc(plan.session_title)}</small></span>
      <span>${label(plan.status)}</span>${chevron}</summary>
      <div class="wake-flow-body"><h3>方案步骤</h3><ul>${rows}</ul>
      ${waits ? `<h3>等待条件</h3><ul>${waits}</ul>` : ''}
      ${receipts ? `<h3>唤醒事件回执</h3><ul>${receipts}</ul>` : ''}
      <a href="/?session=${encodeURIComponent(plan.session_id)}">打开所属对话</a></div></details>`;
  }).join('') || '<p class="queue-message">暂无方案流程</p>';
  const markup = html + (unreadable ? '<p role="status">部分方案记录暂时无法读取。</p>' : '');
  if (container.dataset.markup === markup) return;
  const open = new Set([...container.querySelectorAll('details[open]')].map(el => el.dataset.workflow));
  const focused = document.activeElement.closest('details')?.dataset.workflow;
  container.innerHTML = markup;
  container.dataset.markup = markup;
  for (const details of container.querySelectorAll('details')) {
    details.open = open.has(details.dataset.workflow);
    if (details.dataset.workflow === focused) details.querySelector('summary').focus({preventScroll:true});
  }
}
