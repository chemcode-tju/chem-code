import {escapeHtml} from './core.mjs';

export const kinds = {plan:'计算方案', calculation:'计算记录', lesson:'故障经验', document:'补充文档'};
export const levels = {unverified:'未核验', observed:'已记录', converged:'收敛检查通过'};
const esc = value => escapeHtml(String(value ?? ''));
const valueText = value => value === undefined ? '未记录' : typeof value === 'string' ? value : JSON.stringify(value);
const dateText = value => new Date(value).toLocaleString('zh-CN', {hour12:false});

export function scopeDescription(scope) {
  if (scope === 'session') return '仅检索所选对话沉淀的文档。切换对话后自动更新结果。';
  if (scope === 'project') return '检索所选项目的研究记录；选择来源对话时采用其项目。同一项目内切换对话，结果可能相同。';
  return '检索本人全部项目和对话。切换来源对话不会缩小此范围。';
}

export function resultCard(item) {
  return `<span class="card-meta"><span>${esc(kinds[item.kind] || item.kind)}</span><span>v${esc(item.version)}</span><span class="evidence-tag ${item.evidence?.level === 'converged' ? 'converged' : ''}">${esc(levels[item.evidence?.level] || '未核验')}</span></span>
    <span class="card-title" data-no-i18n>${esc(item.title)}</span><span class="card-source"${item.session_title ? ' data-no-i18n' : ''}>${esc(item.session_title || '研究平台')}</span>`;
}

function section(title, html) {
  return `<section class="document-section"><h3>${esc(title)}</h3>${html}</section>`;
}

function table(headers, rows, preserve = () => false) {
  return `<div class="table-scroll"><table><thead><tr>${headers.map(h=>`<th scope="col">${esc(h)}</th>`).join('')}</tr></thead><tbody>${rows.map((row,rowIndex)=>`<tr>${row.map((value,column)=>`<td${preserve(rowIndex,column) ? ' data-no-i18n' : ''}>${esc(valueText(value))}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
}

function lessonBody(item) {
  try {
    const record = JSON.parse(item.content);
    if (!record || typeof record !== 'object' || !('original_error' in record)) return null;
    const changes = Array.isArray(record.parameter_changes) ? record.parameter_changes.filter(row=>row && typeof row === 'object') : [];
    return section('原始报错', `<p class="error-message"${record.original_error !== undefined ? ' data-no-i18n' : ''}>${esc(valueText(record.original_error))}</p>`)
      + section('当时的修复说明', `<p${record.proposed_reason !== undefined ? ' data-no-i18n' : ''}>${esc(valueText(record.proposed_reason))}</p>`)
      + (changes.length ? section('参数变化 · 修复前 → 修复后', table(['参数','修复前','修复后'], changes.map(row=>[
          row.parameter, row.reference_present === false ? '未显式记录' : row.reference,
          row.current_present === false ? '未显式记录' : row.current,
        ]), (row,column)=>column===0 || (column===1 ? changes[row].reference_present!==false : changes[row].current_present!==false))) : '')
      + section('后续结果', `<p>${esc(({completed:'后续执行完成', failed:'后续执行仍失败', running:'后续执行中'})[record.retry_outcome] || record.retry_outcome || '尚无后续结果')}</p>`);
  } catch { return null; }
}

export function documentBody(item, sourceTitle, markdown) {
  const evidence = item.evidence || {};
  const lesson = item.kind === 'lesson' ? lessonBody(item) : null;
  const check = value => value === true ? '通过' : value === false ? '未通过' : '未记录';
  const evidenceRows = [
    ['电子收敛', check(evidence.electronic_converged)],
    ['离子收敛', check(evidence.ionic_converged)],
    ['科学精度', evidence.scientifically_validated === true ? '有审核记录（需核对来源）' : '未验证'],
  ];
  if (evidence.scheduler_job_id) evidenceRows.push(['调度作业', evidence.scheduler_job_id]);
  if (evidence.exit_status !== undefined && evidence.exit_status !== null) evidenceRows.push(['退出码', evidence.exit_status]);
  const tags = [...(item.systems || []), ...(item.tasks || [])];
  const raw = JSON.stringify({parameters:item.parameters, applicability:item.applicability, evidence, source_key:item.source_key}, null, 2);
  return `<header class="document-header"><div class="card-meta"><span>${esc(kinds[item.kind] || item.kind)}</span><span class="evidence-tag ${evidence.level === 'converged' ? 'converged' : ''}">${esc(levels[evidence.level] || '未核验')}</span><span>v${esc(item.version)}</span></div>
    <h2 data-no-i18n>${esc(item.title)}</h2><p class="document-meta"><span${sourceTitle ? ' data-no-i18n' : ''}>${esc(sourceTitle || '研究平台')}</span> · <time data-no-i18n datetime="${esc(item.updated_at)}">${esc(dateText(item.updated_at))}</time></p></header>
    <div class="actions"><a data-download>下载 Markdown</a>${item.session_id ? `<a href="/?session=${encodeURIComponent(item.session_id)}">来源对话</a>` : ''}<button type="button" data-versions>版本记录</button><button class="forget" type="button" data-forget>移出检索</button></div>
    ${tags.length ? `<p class="document-meta" data-no-i18n>${tags.map(esc).join(' · ')}</p>` : ''}
    ${lesson ?? `<div class="markdown">${markdown.render(item.content)}</div>`}
    ${section('证据与适用性', table(['检查项','记录'], evidenceRows,(row,column)=>row>=3&&column===1))}
    <p class="document-notice"><span${item.applicability ? ' data-no-i18n' : ''}>${esc(item.applicability || '复用前核对体系、参数及来源。')}</span><br>收敛记录不代表科学精度已验证；参数变化不等于已证实的报错根因。</p>
    ${lesson ? `<details class="raw-record"><summary>完整原始记录与代码</summary><pre>${esc(item.content)}</pre></details>` : ''}
    <details class="raw-record"><summary>计算参数与来源标识</summary><pre>${esc(raw)}</pre></details>`;
}
