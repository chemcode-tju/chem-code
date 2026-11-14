import { effortLabel } from "./agent-menu.mjs";

export function createAgentSettings(api) {
  const $ = id => document.getElementById(id);
  const agents = {codex: {name:'Codex', executable:'codex'}, antigravity: {name:'Antigravity', executable:'agy'}};
  let saved = {}, catalogs = {}, loaded = false;
  const config = () => {
    const result = {...saved, default_backend:$('agent-backend').value};
    for (const [id, agent] of Object.entries(agents)) {
      result[`${id}_executable`] = $(`${id}-executable`).value.trim() || agent.executable;
      result[`${id}_model`] = $(`${id}-model`).value;
      result[`${id}_effort`] = $(`${id}-effort`).value;
    }
    return result;
  };
  function markers() {
    document.querySelectorAll('[data-default-for]').forEach(node => {
      node.textContent = node.dataset.defaultFor === $('agent-backend').value ? '默认' : '';
    });
  }
  function efforts(id, selected = '') {
    const model = catalogs[id]?.find(item => item.id === $(`${id}-model`).value);
    const values = [...(model?.efforts || [])];
    if (selected && !values.includes(selected)) values.push(selected);
    $(`${id}-effort`).replaceChildren(new Option('跟随模型', ''), ...values.map(value => new Option(effortLabel(value), value)));
    $(`${id}-effort`).value = selected;
  }
  function modelOptions(id, selected, effort = '') {
    const models = catalogs[id] || [];
    const options = models.map(model => new Option(model.name, model.id));
    if (selected && !models.some(model => model.id === selected)) options.push(new Option(selected, selected));
    $(`${id}-model`).replaceChildren(new Option(`跟随 ${agents[id].name}`, ''), ...options);
    $(`${id}-model`).value = selected; efforts(id, effort);
  }
  async function act(fn) {
    const buttons = [$('agent-save'), ...Object.keys(agents).map(id => $(`${id}-probe`))];
    buttons.forEach(button => button.disabled = true);
    try { await fn(); } catch (error) { $('agent-notice').textContent = error.message; }
    finally { buttons.forEach(button => button.disabled = false); }
  }
  for (const id of Object.keys(agents)) {
    $(`${id}-model`).addEventListener('change', () => efforts(id));
    $(`${id}-probe`).addEventListener('click', () => act(async () => {
      $('agent-notice').textContent = '正在连接…';
      const selected = $(`${id}-model`).value, effort = $(`${id}-effort`).value;
      const result = await api(`/api/agents/${id}/probe`, {method:'POST', body:JSON.stringify(config())});
      $('agent-notice').textContent = result.message;
      if (result.executable) $(`${id}-executable`).value = result.executable;
      catalogs[id] = result.models || []; modelOptions(id, selected, effort);
    }));
    $(`${id}-settings`).addEventListener('toggle', () => {
      if ($(`${id}-settings`).open && loaded && !catalogs[id]) $(`${id}-probe`).click();
    });
  }
  $('agent-backend').addEventListener('change', markers);
  $('agent-save').addEventListener('click', () => act(async () => {
    saved = await api('/api/agents/settings', {method:'PUT', body:JSON.stringify(config())});
    $('agent-notice').textContent = '已保存';
  }));
  return { async load() {
    if (loaded) return;
    await act(async () => {
      const result = await api('/api/agents/settings'); saved = result.settings;
      $('agent-backend').value = saved.default_backend;
      for (const id of Object.keys(agents)) {
        $(`${id}-executable`).value = saved[`${id}_executable`] || agents[id].executable;
        modelOptions(id, saved[`${id}_model`] || '', saved[`${id}_effort`] || '');
      }
      markers(); loaded = true;
    });
  }};
}
