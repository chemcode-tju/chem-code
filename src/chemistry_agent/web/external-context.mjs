// Render only native measurements. Lifetime/billed usage never enters this view.
export function externalContextView(info, locale = 'zh-CN') {
  const number = value => Number.isInteger(value) && value >= 0;
  const used = number(info.used_tokens) ? info.used_tokens : null;
  const capacity = number(info.capacity_tokens) && info.capacity_tokens > 0 ? info.capacity_tokens : null;
  const format = value => value.toLocaleString(locale);
  const percent = used !== null && capacity ? Math.round(1000 * used / capacity) / 10 : null;
  const rows = [];
  if (used !== null) rows.push(['上下文', `${format(used)}${capacity ? ` / ${format(capacity)}` : ''} tokens`]);
  else if (capacity) rows.push(['上下文窗口', `${format(capacity)} tokens`]);
  const operation = info.operation || {};
  const compact = operation.kind === 'compact' && ['failed', 'interrupted'].includes(operation.state) &&
    (!info.compaction?.finished_at || info.compaction.finished_at < operation.started_at)
    ? {status:operation.state} : info.compaction || {};
  const labels = {running:'正在压缩', completed:'压缩完成', failed:'压缩失败', interrupted:'压缩连接中断'};
  if (labels[compact.status]) rows.push(['压缩', labels[compact.status]]);
  if (compact.status === 'completed' && number(compact.before_tokens) && number(compact.after_tokens)) {
    rows.push(['压缩前后', `${format(compact.before_tokens)} → ${format(compact.after_tokens)} tokens`]);
  }
  const date = compact.finished_at || compact.started_at || (used !== null ? info.updated_at : null);
  if (date && !Number.isNaN(Date.parse(date))) rows.push(['更新于', new Date(date).toLocaleString(locale)]);
  return {rows, percent, visible: rows.length > 0 || info.manual_supported === true,
    manual: info.manual_supported === true,
    disabled: info.busy === true || compact.status === 'running',
    title: `${info.managed_by === 'codex' ? 'Codex' : 'Antigravity'} · 上下文`};
}
