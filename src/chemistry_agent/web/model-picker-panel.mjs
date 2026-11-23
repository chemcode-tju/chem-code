import {createAnchoredPanel, effortLabel} from './agent-menu.mjs';
import {t} from './i18n.mjs';

const effortOrder = new Map([
  '', 'disabled', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'extra', 'max', 'ultra', 'enabled',
].map((effort, rank) => [effort, rank]));

export function createModelPickerPanel(anchor, {getAgents, getSelection, agentChoice, catalog, choose, openSettings}) {
  const panel = document.createElement('section');
  panel.id = 'conversation-model-panel';
  panel.className = 'agent-menu model-picker-panel';
  panel.setAttribute('role', 'dialog');
  panel.innerHTML = `
    <section class="model-picker-agents">
      <h2>Agent</h2>
      <div data-agents role="group" aria-label="Agent"></div>
    </section>
    <section class="model-picker-models">
      <label class="sr-only" for="conversation-model-search">搜索模型</label>
      <input id="conversation-model-search" type="search" placeholder="搜索模型" autocomplete="off">
      <div class="model-picker-options" data-models role="group" aria-label="模型"></div>
      <p class="model-picker-message" role="status"></p>
      <form class="menu-manual" hidden>
        <input maxlength="160" placeholder="模型 ID" aria-label="模型 ID" required>
        <button type="submit" class="secondary">应用</button>
      </form>
    </section>
    <section class="model-picker-reasoning">
      <h2>推理强度</h2>
      <div class="model-picker-options" data-efforts role="group" aria-label="推理强度"></div>
    </section>
    <footer class="model-picker-footer">
      <button type="button" class="quiet-button" data-refresh>刷新模型</button>
      <button type="button" class="quiet-button" data-settings>配置 Agent…</button>
    </footer>`;
  const popup = createAnchoredPanel(anchor, panel);
  const agents = panel.querySelector('[data-agents]');
  const models = panel.querySelector('[data-models]');
  const efforts = panel.querySelector('[data-efforts]');
  const search = panel.querySelector('input[type=search]');
  const notice = panel.querySelector('.model-picker-message');
  const manual = panel.querySelector('form');
  let instance, data, request = 0, busy = false;

  function option(label, active, action, translate = true) {
    const button = document.createElement('button');
    button.type = 'button';
    button.setAttribute('aria-pressed', String(active));
    button.className = 'model-picker-option';
    const name = document.createElement('span');
    name.textContent = translate ? t(label) : label;
    if (!translate) name.dataset.noI18n = '';
    button.title = name.textContent;
    button.append(name);
    if (active) button.insertAdjacentHTML('beforeend', '<svg class="icon" aria-hidden="true"><use href="/assets/ui-icons.svg#i-check"/></svg>');
    button.onclick = action;
    return button;
  }

  const agentId = selection => selection.instance_id || `local-${selection.backend}`;
  const browsingCurrentAgent = () => agentId(getSelection()) === instance.id;

  function currentForInstance() {
    return browsingCurrentAgent() ? getSelection() : agentChoice(instance);
  }

  function drawOptions() {
    if (!instance) return;
    const base = currentForInstance();
    const available = data?.models || [];
    const current = available.find(item => item.id === base.model) || (!base.model ? available.find(item => item.default) : null);
    const searchable = base.model && !current ? [{id:base.model}, ...available] : available;
    const query = search.value.trim().toLocaleLowerCase();
    const rows = searchable.filter(model => [model.name, model.id].some(value => value?.toLocaleLowerCase().includes(query)));
    models.replaceChildren(
      ...rows.map(model => option(model.name || model.id, base.model === model.id, () => setModel(model), false)),
      option('跟随默认模型', !base.model, () => setModel(null)),
    );
    const values = [...new Set(['', ...(current?.efforts || []), base.effort || ''])]
      .sort((left, right) => (effortOrder.get(left) ?? effortOrder.size) - (effortOrder.get(right) ?? effortOrder.size));
    efforts.replaceChildren(...values.map(effort => {
      const button = option(effortLabel(effort), (base.effort || '') === effort, () => apply({...base, effort}, efforts, effort));
      button.dataset.value = effort;
      button.disabled = Boolean(effort && !current?.efforts?.includes(effort));
      return button;
    }));
    rows.forEach((row, index) => { models.children[index].dataset.value = row.id; });
    models.lastElementChild.dataset.value = '';
    manual.hidden = instance.backend !== 'builtin' || data?.provider === 'chatgpt';
    popup.reposition();
  }

  function setModel(model) {
    const base = currentForInstance();
    const next = !model && !browsingCurrentAgent() ? base : {
      ...base, model:model?.id || '', effort:model?.efforts?.includes(base.effort) ? base.effort : '',
    };
    void apply(next, models, next.model || '');
  }

  async function apply(next, list, value) {
    if (busy) return;
    busy = true;
    panel.setAttribute('aria-busy', 'true');
    try {
      await choose(next);
      if (popup.visible) {
        drawOptions();
        ([...list.children].find(button => button.dataset.value === value && !button.disabled) || search).focus();
      }
    } finally {
      busy = false;
      panel.removeAttribute('aria-busy');
      if (popup.visible && anchor.disabled) popup.close();
    }
  }

  async function selectAgent(agent, reload = false) {
    if (busy) return;
    instance = agent;
    const version = ++request;
    data = null;
    if (!reload) { search.value = ''; manual.reset(); }
    const restoreAgentFocus = agents.contains(document.activeElement);
    agents.replaceChildren(...getAgents().map(item => option(item.name, item.id === agent.id, () => selectAgent(item), false)));
    if (restoreAgentFocus) agents.querySelector('[aria-pressed=true]')?.focus();
    notice.textContent = t('正在读取模型…');
    drawOptions();
    try {
      const result = await catalog(agent.id, reload);
      if (version !== request || !popup.visible) return;
      data = result;
      notice.textContent = result.message && !result.authenticated && agent.backend !== 'builtin' ? result.message : '';
    } catch (error) {
      if (version !== request || !popup.visible) return;
      notice.textContent = error.message;
    }
    if (version === request && popup.visible) {
      const active = document.activeElement;
      const focusedList = [models, efforts].find(list => list.contains(active));
      const value = active.dataset.value;
      drawOptions();
      if (focusedList) ([...focusedList.children].find(button => button.dataset.value === value && !button.disabled) || search).focus();
    }
  }

  search.addEventListener('input', drawOptions);
  search.addEventListener('keydown', event => {
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      models.querySelector('button')?.focus();
    }
  });
  for (const list of [agents, models, efforts]) list.addEventListener('keydown', event => {
    const step = {ArrowDown:1, ArrowUp:-1}[event.key];
    if (!step) return;
    const buttons = [...list.querySelectorAll('button:not(:disabled)')];
    if (!buttons.length) return;
    event.preventDefault();
    const index = buttons.indexOf(document.activeElement);
    buttons[(index + step + buttons.length) % buttons.length].focus();
  });

  manual.onsubmit = event => {
    event.preventDefault();
    const model = manual.elements[0].value.trim();
    if (model) setModel({id:model});
  };
  panel.querySelector('[data-refresh]').onclick = () => { if (instance) void selectAgent(instance, true); };
  panel.querySelector('[data-settings]').onclick = () => { popup.close(true); openSettings(); };

  return {
    get visible() { return popup.visible; },
    close: popup.close,
    open() {
      panel.setAttribute('aria-label', t('模型选择'));
      panel.querySelectorAll('h2').forEach(heading => { heading.textContent = t(heading.textContent); });
      search.placeholder = t('搜索模型');
      popup.open();
      const selection = getSelection();
      const id = agentId(selection);
      const list = getAgents();
      const agent = list.find(item => item.id === id) || list[0];
      if (agent) void selectAgent(agent);
      else {
        agents.replaceChildren(); models.replaceChildren(); efforts.replaceChildren(); manual.hidden = true;
        notice.textContent = t('此主机尚未添加 Agent');
      }
      search.focus();
    },
  };
}
