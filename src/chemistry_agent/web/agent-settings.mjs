import { effortLabel } from "./agent-menu.mjs";
import {t} from "./i18n.mjs";

export function createAgentSettings(api) {
  const $ = id => document.getElementById(id);
  const agents = {codex: {name:'Codex', executable:'codex'}, antigravity: {name:'Antigravity', executable:'agy'}};
  let saved = {}, catalogs = {}, descriptors = {}, probes = {}, loaded = false, busy = false, builtin = null;
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
      node.hidden = node.dataset.defaultFor !== saved.default_backend;
      node.textContent = t('默认');
    });
    document.querySelectorAll('[data-use-backend]').forEach(button => {
      const selected = button.dataset.useBackend === saved.default_backend;
      button.textContent = t(selected ? '已设为默认' : '设为新对话默认');
      button.setAttribute('aria-pressed', String(selected));
      button.disabled = busy || !loaded || selected;
    });
    for (const [id, agent] of Object.entries(agents)) {
      const model = saved[`${id}_model`];
      const label = catalogs[id]?.find(item => item.id === model)?.name || model || t(`跟随 ${agent.name}`);
      const executable = saved[`${id}_executable`] || agent.executable;
      const state = probes[id]?.paths.includes(executable) ? probes[id] : null;
      const descriptor = descriptors[id];
      const status = t(state ? (state.authenticated ? '已验证登录' : state.available ? '未通过验证' : '未安装')
        : descriptor?.executable !== executable ? '尚未检测' : descriptor?.available ? '已安装 · 尚未检测' : '未找到程序');
      const summary = document.querySelector(`[data-model-for="${id}"]`);
      summary.textContent = `${label} · ${status}`;
      summary.title = summary.textContent;
      summary.setAttribute('data-no-i18n', '');
      const connection = document.querySelector(`[data-connection-for="${id}"]`);
      const edited = $(`${id}-executable`).value.trim() || agent.executable;
      const currentProbe = probes[id]?.paths.includes(edited) ? probes[id] : null;
      connection.textContent = currentProbe ? t(currentProbe.authenticated ? '已验证登录' : currentProbe.available ? '未通过验证' : '未安装')
        : edited !== executable ? t('程序路径已修改 · 需要重新检测') : status;
      connection.dataset.state = currentProbe && !currentProbe.authenticated ? 'failed' : '';
      connection.hidden = connection.textContent === status;
    }
  }
  function builtinSummary(data) {
    builtin = data;
    const summary = document.querySelector('[data-model-for="builtin"]');
    const provider = {chatgpt:'ChatGPT', deepseek:'DeepSeek', 'openai-compatible':'OpenAI 兼容'}[data.provider] || 'API 与账号';
    summary.textContent = `${t(provider)} · ${data.model || t('自动选择模型')}${typeof data.configured === 'boolean' ? ' · '+t(data.configured ? '已配置' : '未配置') : ''}`;
    summary.title = summary.textContent;
    summary.setAttribute('data-no-i18n', '');
  }
  document.addEventListener('chem:model-settings', event => builtinSummary(event.detail));
  function efforts(id, selected = '') {
    const model = catalogs[id]?.find(item => item.id === $(`${id}-model`).value);
    const values = [...(model?.efforts || [])];
    if (selected && !values.includes(selected)) values.push(selected);
    $(`${id}-effort`).replaceChildren(new Option(t('跟随模型'), ''), ...values.map(value => new Option(effortLabel(value), value)));
    $(`${id}-effort`).value = selected;
  }
  function modelOptions(id, selected, effort = '') {
    const models = catalogs[id] || [];
    const option = (label, value) => { const node = new Option(label, value); node.dataset.noI18n = ''; return node; };
    const options = models.map(model => option(model.name, model.id));
    if (selected && !models.some(model => model.id === selected)) options.push(option(selected, selected));
    $(`${id}-model`).replaceChildren(new Option(t(`跟随 ${agents[id].name}`), ''), ...options);
    $(`${id}-model`).value = selected; efforts(id, effort);
  }
  async function act(fn) {
    if (busy) return;
    busy = true;
    const buttons = [$('agent-save'), ...Object.keys(agents).map(id => $(`${id}-probe`))];
    buttons.forEach(button => button.disabled = true);
    document.querySelectorAll('[data-use-backend]').forEach(button => button.disabled = true);
    $('agent-notice').classList.remove('error');
    try { await fn(); } catch (error) {
      $('agent-notice').textContent = error.message;
      $('agent-notice').classList.add('error');
    } finally {
      busy = false;
      buttons.forEach(button => button.disabled = !loaded);
      markers();
    }
  }
  for (const id of Object.keys(agents)) {
    $(`${id}-model`).addEventListener('change', () => efforts(id));
    $(`${id}-executable`).addEventListener('input', () => {
      delete probes[id];
      markers();
    });
    $(`${id}-probe`).addEventListener('click', () => act(async () => {
      $('agent-notice').textContent = '正在检测…';
      const selected = $(`${id}-model`).value, effort = $(`${id}-effort`).value, payload = config();
      let result;
      try {
        result = await api(`/api/agents/${id}/probe`, {method:'POST', body:JSON.stringify(payload)});
      } catch (error) {
        probes[id] = {available:true, authenticated:false, paths:[payload[`${id}_executable`]]};
        throw error;
      }
      probes[id] = {...result, paths:[payload[`${id}_executable`],result.executable].filter(Boolean)};
      $('agent-notice').textContent = result.message;
      if (result.executable) $(`${id}-executable`).value = result.executable;
      catalogs[id] = result.models || []; modelOptions(id, selected, effort);
    }));
  }
  document.querySelectorAll('[data-use-backend]').forEach(button => button.addEventListener('click', () => act(async () => {
    // A default affects new chats only. Do not save unrelated edits or change active sessions.
    saved = await api('/api/agents/settings', {method:'PUT', body:JSON.stringify({...saved, default_backend:button.dataset.useBackend})});
    $('agent-backend').value = saved.default_backend;
    $('agent-notice').textContent = '已设置新对话默认 Agent';
  })));
  $('agent-save').addEventListener('click', () => act(async () => {
    saved = await api('/api/agents/settings', {method:'PUT', body:JSON.stringify(config())});
    $('agent-notice').textContent = '已保存模型设置';
  }));
  window.addEventListener('languagechange', () => {markers();if(builtin) builtinSummary(builtin);});
  return { async load() {
    if (loaded) return;
    await act(async () => {
      const result = await api('/api/agents/settings'); saved = result.settings;
      descriptors = Object.fromEntries((result.backends || []).map(item => [item.id, {...item,executable:saved[`${item.id}_executable`] || agents[item.id]?.executable}]));
      $('agent-backend').value = saved.default_backend;
      for (const id of Object.keys(agents)) {
        $(`${id}-executable`).value = saved[`${id}_executable`] || agents[id].executable;
        modelOptions(id, saved[`${id}_model`] || '', saved[`${id}_effort`] || '');
      }
      loaded = true;
    });
  }};
}
