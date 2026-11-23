import {escapeHtml as esc} from './core.mjs';
import {effortLabel} from './agent-menu.mjs';
import {t} from './i18n.mjs';

export function presetDescription(preset) {
  if (!preset) return '';
  const backend = preset.backend === 'codex' ? 'Codex' : 'Antigravity';
  return [backend, preset.model, preset.effort && t(effortLabel(preset.effort))].filter(Boolean).join(' · ');
}

export function taskAgentBar(node) {
  if (!node.agentSelection) return '';
  const {editable, assigned, options} = node.agentSelection;
  const fallback = t('主对话 Agent');
  const description = presetDescription(assigned) || fallback;
  const label = t('执行 Agent');
  const rows = options.filter(preset => preset.enabled);
  let control = `<span class="task-agent-value" data-no-i18n>${esc(assigned?.name || fallback)}</span>`;
  if (editable && (rows.length || assigned)) {
    const chosen = assigned ? `${assigned.id}:${assigned.version}` : '';
    const current = assigned && !rows.some(item => `${item.id}:${item.version}` === chosen)
      ? `<option value="${esc(chosen)}" selected data-no-i18n>${esc(assigned.name)} · v${assigned.version}</option>` : '';
    control = `<select data-task-agent="${esc(node.id)}" data-assignment="${esc(chosen)}" aria-label="${esc(label + ' · ' + node.title)}">
      <option value="">${esc(fallback)}</option>${current}${rows.map(item => {
        const value = `${item.id}:${item.version}`;
        const label = `${item.name} · ${presetDescription(item)}`;
        return `<option value="${esc(value)}" ${chosen === value ? 'selected' : ''} data-menu-label="${esc(label)}" data-no-i18n>${esc(item.name)}</option>`;
      }).join('')}</select>`;
  }
  return `<div class="task-agent-bar" title="${esc(description)}"><span>${esc(label)}</span>${control}</div>`;
}
