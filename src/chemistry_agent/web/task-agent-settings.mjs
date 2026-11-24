import {escapeHtml as esc} from './core.mjs';
import {effortLabel} from './agent-menu.mjs';
import {presetDescription} from './task-agent-control.mjs';
import {t} from './i18n.mjs';

export function createTaskAgentSettings(api) {
  const section = document.getElementById('task-agent-settings');
  const form = section.querySelector('form');
  const fields = form.elements;
  const notice = section.querySelector('[role="status"]');
  const rows = section.querySelector('[data-presets]');
  const save = form.querySelector('button[type="submit"]');
  let items = [], instances = [], editing = null, catalog = [], requestVersion = 0;

  function draw() {
    rows.innerHTML = items.map(item => `<div class="task-preset-row">
      <span class="agent-identity"><strong data-no-i18n>${esc(item.name)}</strong><span class="agent-model" data-no-i18n>${esc(presetDescription(item))}${item.enabled ? '' : ` · ${esc(t('已停用'))}`}</span></span>
      <button type="button" class="secondary" data-edit-preset="${esc(item.id)}">编辑</button>
      ${item.enabled ? `<button type="button" class="quiet-button" data-disable-preset="${esc(item.id)}">停用</button>` : ''}
    </div>`).join('') || '<p class="task-preset-empty">暂无执行预设</p>';
  }

  async function load(hostingData) {
    const [presets, hosting] = await Promise.all([api('/api/agents/presets'), hostingData || api('/api/hosting')]);
    items = presets.items;
    instances = hosting.agents.filter(item => item.enabled && ['builtin','codex','antigravity'].includes(item.backend));
    draw();
  }

  function efforts(selected = '') {
    const model = catalog.find(item => item.id === fields.model.value);
    fields.effort.innerHTML = `<option value="">${esc(t('跟随模型'))}</option>` + (model?.efforts || []).map(item => {
      const id = typeof item === 'string' ? item : item.id;
      return `<option value="${esc(id)}">${esc(t(effortLabel(id)))}</option>`;
    }).join('');
    fields.effort.value = [...fields.effort.options].some(option => option.value === selected) ? selected : '';
  }

  async function models(model = '', effort = '') {
    const generation = ++requestVersion;
    catalog = [];
    fields.model.replaceChildren();
    efforts();
    save.disabled = true;
    if (!fields.instance.value) return;
    notice.textContent = t('正在读取模型…');
    try {
      const response = await api(`/api/hosting/agents/${encodeURIComponent(fields.instance.value)}/models`);
      if (generation !== requestVersion) return;
      if (!response.authenticated || !response.models?.length) throw Error(response.message || t('请先连接 Agent 并完成登录。'));
      catalog = response.models;
      fields.model.innerHTML = catalog.map(item => `<option data-no-i18n value="${esc(item.id)}">${esc(item.name || item.id)}</option>`).join('');
      fields.model.value = catalog.find(item => item.id === model)?.id || catalog.find(item => item.default)?.id || catalog[0].id;
      efforts(effort);
      save.disabled = false;
      notice.textContent = '';
    } catch (error) {
      if (generation === requestVersion) notice.textContent = error.message;
    }
  }

  function edit(item = null) {
    editing = item;
    form.hidden = false;
    fields.name.value = item?.name || '';
    fields.instructions.value = item?.instructions || '';
    fields.instance.innerHTML = instances.map(instance => `<option data-no-i18n value="${esc(instance.id)}">${esc(instance.name)}${instance.host_id === 'local' ? '' : ` · ${esc(instance.host_id)}`}</option>`).join('');
    fields.instance.value = item ? item.instance_id : instances[0]?.id || '';
    fields.name.focus();
    void models(item?.model, item?.effort);
  }

  async function persist(id, body) {
    const item = await api(`/api/agents/presets/${id}`, {method:'PUT',body:JSON.stringify(body)});
    items = [...items.filter(row => row.id !== id), item];
    draw();
  }

  section.querySelector('[data-add-preset]').onclick = async () => {
    try {await load(); edit();} catch (error) {notice.textContent = error.message;}
  };
  rows.onclick = async event => {
    const button = event.target.closest('button');
    const item = items.find(row => row.id === (button?.dataset.editPreset || button?.dataset.disablePreset));
    if (!item) return;
    if (button.dataset.editPreset) return edit(item);
    const {id, version, provider, destination, ...body} = item;
    button.disabled = true;
    try {await persist(id, {...body,enabled:false});} catch (error) {notice.textContent = error.message;}
    finally {button.disabled = false;}
  };
  fields.instance.onchange = () => models();
  fields.model.onchange = () => efforts();
  form.querySelector('[data-cancel-preset]').onclick = () => {requestVersion++; form.hidden = true; notice.textContent = '';};
  form.onsubmit = async event => {
    event.preventDefault();
    const instance = instances.find(item => item.id === fields.instance.value);
    if (!instance || save.disabled) return;
    save.disabled = true;
    try {
      await persist(editing?.id || crypto.randomUUID(), {
        name:fields.name.value,backend:instance.backend,instance_id:instance.id,
        model:fields.model.value,effort:fields.effort.value,instructions:fields.instructions.value,enabled:true,
      });
      form.hidden = true;
      notice.textContent = t('已保存执行预设');
      section.querySelector('[data-add-preset]').focus();
    } catch (error) {notice.textContent = error.message;}
    finally {save.disabled = false;}
  };
  window.addEventListener('languagechange', () => { if (rows.firstChild) draw(); });
  return {load:hosting => load(hosting).catch(error => {notice.textContent = error.message;})};
}
