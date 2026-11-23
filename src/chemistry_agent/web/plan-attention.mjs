import {escapeHtml as esc} from './core.mjs';
import {t} from './i18n.mjs';

const labels = {
  approval:'待审批',
  submission:'提交待核对',
  unconverged:'NEB 未收敛',
  needs_review:'NEB 证据待核验',
};
export const attentionLabel = item => t(labels[item.science?.status || item.kind]);
const job = item => item.scheduler_job_id ? `#${item.scheduler_job_id}` : '';
const key = item => item.approval_id || item.job_id;

export function drawPlanAttention(panel, {items, error}) {
  const section = panel.querySelector('[data-plan-attention]');
  if (!section) return;
  panel.querySelector('[data-attention-count]').textContent = items?.length || '';
  const rows = items?.map(item => `<li><button type="button" class="plan-attention-row" data-attention-item="${esc(key(item))}">
    <span><strong data-no-i18n>${esc(item.title)}</strong><small data-no-i18n>${esc(item.request_title || [job(item),item.host].filter(Boolean).join(' · '))}</small></span>
    <span class="plan-attention-label">${esc(attentionLabel(item))}</span></button></li>`).join('');
  const state = error || (!items ? t('正在读取待处理事项…') : !rows ? t('当前方案没有待处理事项。') : '');
  const next = `${state ? `<p class="plan-attention-note" role="status">${esc(state)}</p>` : ''}${rows ? `<ul>${rows}</ul>` : ''}`;
  if (section.dataset.content !== next) {
    const id = section.contains(document.activeElement) && document.activeElement.dataset.attentionItem;
    section.innerHTML = next;
    section.dataset.content = next;
    if (id) (section.querySelector(`[data-attention-item="${CSS.escape(id)}"]`)
      || panel.querySelector('[data-plan-view="attention"]'))?.focus({preventScroll:true});
  }
}

export function stepAttention(items) {
  return items.filter(item => item.kind !== 'approval').map(item => {
    if (item.kind === 'submission') return `<article class="step-attention-item" data-step-attention-id="${esc(item.job_id)}">
      <h3>${esc(attentionLabel(item))}</h3><p>${t('提交回执未确认。核对将查询原提交记录。')}</p>
      <p class="step-attention-meta" data-no-i18n>${esc([item.job_title,item.host].filter(Boolean).join(' · '))}</p>
      <button type="button" class="secondary" data-reconcile-job="${esc(item.job_id)}">${t('核对原提交')}</button>
      <details><summary>${t('原提交记录')}</summary><pre data-no-i18n>${esc([item.submission_name,item.remote_directory].filter(Boolean).join('\n'))}</pre></details>
    </article>`;
    const science = item.science;
    const observed = Number.isFinite(science.force) ? `Fmax ${science.force} eV/Å · ` : '';
    const facts = [job(item),item.scheduler_state?.toUpperCase()].filter(Boolean).join(' · ');
    return `<article class="step-attention-item" data-step-attention-id="${esc(item.job_id)}">
      <h3>${esc(attentionLabel(item))}</h3><p>${t('下游等待通过验收的 NEB 结果。')}</p>
      <p class="step-attention-meta" data-no-i18n>${esc(facts)}</p>
      <p>${esc(observed)}${t('投影力阈值')} ${esc(science.target)} eV/Å</p>
      ${science.source ? `<details><summary>${t('路径证据')}</summary><code>${esc(science.source)}</code></details>` : ''}
    </article>`;
  }).join('');
}
