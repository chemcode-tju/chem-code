import { effortLabel } from "./agent-menu.mjs";
import {t} from "./i18n.mjs";
import {readConnectionCheck, saveConnectionCheck, clearConnectionCheck, checkLifetime} from './connection-checks.mjs';

export function backendCardPatch(values, makeDefault=false) {
  return {executable:values.executable,model:values.model,effort:values.effort,make_default:makeDefault};
}

export function createAgentSettings(api, {saveBuiltin} = {}) {
  const $ = id => document.getElementById(id);
  const agents = {codex: {name:'Codex', executable:'codex'}, antigravity: {name:'Antigravity', executable:'agy'}};
  let saved = {}, catalogs = {}, descriptors = {}, probes = {}, loaded = false, busy = false, builtin = null, builtinDirty = false;
  let noticeTimer;
  let checkStorage;
  try { checkStorage = window.sessionStorage; } catch {}
  function notify(message='', error=false, temporary=true) {
    clearTimeout(noticeTimer);
    $('agent-notice').textContent=message;
    $('agent-notice').classList.toggle('error',error);
    if(message && temporary) noticeTimer=setTimeout(()=>{$('agent-notice').textContent='';},error?8000:5000);
  }
  function currentCheck(id, executable) {
    const probe=probes[id];
    return probe?.paths.includes(executable) && Date.now()-probe.checkedAt<checkLifetime ? probe : null;
  }
  const values = id => ({executable:$(`${id}-executable`).value.trim() || agents[id].executable,
    model:$(`${id}-model`).value,effort:$(`${id}-effort`).value});
  const dirty = id => Object.entries(values(id)).some(([key,value])=>value!==(saved[`${id}_${key}`] || (key==='executable'?agents[id].executable:'')));
  const config = id => ({...saved,...Object.fromEntries(Object.entries(values(id)).map(([key,value])=>[`${id}_${key}`,value]))});
  function updateExecutableOptions(id, info = {}) {
    const list = $(`${id}-executable-options`);
    const input = $(`${id}-executable`);
    if (!input) return;
    const candidates = Array.isArray(info.candidates) ? info.candidates : (descriptors[id]?.candidates || []);
    const resolved = info.resolved_executable || info.executable || descriptors[id]?.resolved_executable || '';
    const resolvedItem = candidates.find(item => item.path === resolved);
    const resolvedVersion = info.version || resolvedItem?.version || '';
    if (list) {
      const options = [];
      const autoOption = document.createElement('option');
      autoOption.value = agents[id].executable;
      autoOption.label = resolved
        ? `${t('自动选择最新版本')} · ${resolved}${resolvedVersion ? ` (${resolvedVersion})` : ''}`
        : t('自动选择最新版本');
      autoOption.dataset.noI18n = '';
      options.push(autoOption);
      for (const item of candidates) {
        if (!item?.path) continue;
        const opt = document.createElement('option');
        opt.value = item.path;
        opt.label = item.version ? `${item.version}` : item.path;
        opt.dataset.noI18n = '';
        options.push(opt);
      }
      list.replaceChildren(...options);
    }
    const edited = input.value.trim() || agents[id].executable;
    input.title = edited === agents[id].executable && resolved
      ? `${t('当前自动解析')}：${resolved}${resolvedVersion ? ` (${resolvedVersion})` : ''}`
      : '';
  }
  function markers() {
    document.querySelectorAll('[data-default-for]').forEach(node => {
      node.hidden = node.dataset.defaultFor !== saved.default_backend;
      node.textContent = t('默认');
    });
    document.querySelectorAll('[data-default-choice]').forEach(input => {
      const selected = input.dataset.defaultChoice === saved.default_backend;
      input.checked = selected;
      input.disabled = busy || !loaded;
    });
    document.querySelector('[data-dirty-for="builtin"]').hidden=!builtinDirty;
    for (const [id, agent] of Object.entries(agents)) {
      document.querySelector(`[data-dirty-for="${id}"]`).hidden=!loaded || !dirty(id);
      $(`${id}-save`).disabled=busy || !loaded || !dirty(id);
      const model = saved[`${id}_model`];
      const label = catalogs[id]?.find(item => item.id === model)?.name || model || t(`跟随 ${agent.name}`);
      const executable = saved[`${id}_executable`] || agent.executable;
      const state = currentCheck(id,executable);
      const descriptor = descriptors[id];
      const status = t(state ? (state.authenticated ? '上次测试通过' : '上次测试失败')
        : descriptor?.executable !== executable ? '尚未检测' : descriptor?.available ? '已安装 · 尚未检测' : '未找到程序');
      const summary = document.querySelector(`[data-model-for="${id}"]`);
      const stateLabel=document.createElement('span');
      stateLabel.className='connection-state';stateLabel.dataset.state=state ? (state.authenticated?'passed':'failed') : 'unknown';stateLabel.textContent=status;
      summary.replaceChildren(document.createTextNode(label+' · '),stateLabel);
      summary.title = summary.textContent;
      summary.dataset.state=state ? (state.authenticated?'passed':'failed') : 'unknown';
      if(state) summary.title += ' · '+new Date(state.checkedAt).toLocaleString(document.documentElement.lang);
      summary.setAttribute('data-no-i18n', '');
      const connection = document.querySelector(`[data-connection-for="${id}"]`);
      const edited = $(`${id}-executable`).value.trim() || agent.executable;
      const currentProbe = currentCheck(id,edited);
      connection.textContent = currentProbe ? t(currentProbe.authenticated ? '上次测试通过' : '上次测试失败')
        : edited !== executable ? t('程序路径已修改 · 需要重新检测') : status;
      connection.dataset.state = currentProbe ? (currentProbe.authenticated?'passed':'failed') : 'unknown';
      connection.title=currentProbe ? new Date(currentProbe.checkedAt).toLocaleString(document.documentElement.lang) : '';
      if(currentProbe) connection.textContent += ' · '+new Date(currentProbe.checkedAt).toLocaleTimeString(document.documentElement.lang,{hour:'2-digit',minute:'2-digit'});
      connection.hidden = false;
      updateExecutableOptions(id);
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
  document.addEventListener('chem:model-dirty', event => {builtinDirty=event.detail;markers();});
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
    const buttons = Object.keys(agents).flatMap(id => [$(`${id}-save`),$(`${id}-probe`)]);
    buttons.forEach(button => button.disabled = true);
    document.querySelectorAll('[data-default-choice]').forEach(button => button.disabled = true);
    notify();
    try { await fn(); } catch (error) {
      notify(error.message,true);
    } finally {
      busy = false;
      buttons.forEach(button => button.disabled = !loaded);
      markers();
    }
  }
  for (const id of Object.keys(agents)) {
    $(`${id}-model`).addEventListener('change', () => {efforts(id);markers();});
    $(`${id}-effort`).addEventListener('change', markers);
    $(`${id}-executable`).addEventListener('input', () => {
      markers();
    });
    $(`${id}-probe`).addEventListener('click', () => act(async () => {
      notify(t('正在检测…'),false,false);
      const selected = $(`${id}-model`).value, effort = $(`${id}-effort`).value, payload = config(id);
      let result;
      try {
        result = await api(`/api/agents/${id}/probe`, {method:'POST', body:JSON.stringify(payload)});
      } catch (error) {
        probes[id] = saveConnectionCheck(checkStorage,id,payload[`${id}_executable`],{authenticated:false,paths:[payload[`${id}_executable`]]});
        throw error;
      }
      probes[id] = saveConnectionCheck(checkStorage,id,payload[`${id}_executable`],{...result,paths:[payload[`${id}_executable`],result.executable].filter(Boolean)});
      if (descriptors[id]) {
        descriptors[id] = {
          ...descriptors[id],
          available: result.available ?? descriptors[id].available,
          resolved_executable: result.executable || descriptors[id].resolved_executable,
          candidates: Array.isArray(result.candidates) ? result.candidates : descriptors[id].candidates,
        };
      }
      const resolvedDetail = result.executable
        ? ` (${result.executable}${result.version ? ` · ${result.version}` : ''})`
        : '';
      notify(t(result.message) + resolvedDetail, !result.authenticated);
      if (result.executable && payload[`${id}_executable`] !== agents[id].executable) {
        $(`${id}-executable`).value = result.executable;
      }
      updateExecutableOptions(id, result);
      catalogs[id] = result.models || []; modelOptions(id, selected, effort);
    }));
    $(`${id}-save`).addEventListener('click',()=>act(()=>saveCard(id)));
  }
  async function saveCard(id, makeDefault=false) {
    // Built-in model credentials have their own API. A failed default save stays visibly failed.
    const builtinSaved=id==='builtin' && builtinDirty;
    if(builtinSaved) await saveBuiltin();
    const body=id==='builtin'?{make_default:makeDefault}:backendCardPatch(values(id),makeDefault);
    try {saved=await api(`/api/agents/settings/${id}`,{method:'PATCH',body:JSON.stringify(body)});}
    catch(error) {
      if(builtinSaved)throw new Error(t('Chem Agent 配置已保存，但默认 Agent 未更新。')+' '+error.message);
      throw error;
    }
    if(agents[id]) {
      const path=saved[`${id}_executable`],probe=currentCheck(id,path);
      if(probe)probes[id]=saveConnectionCheck(checkStorage,id,path,probe,probe.checkedAt);
      else clearConnectionCheck(checkStorage,id);
    }
    notify(t(makeDefault?'配置已保存，并设为新对话默认':'已保存模型设置'));
    document.dispatchEvent(new Event('chem:agent-settings-updated'));
  }
  document.querySelectorAll('[data-default-choice]').forEach(input=>input.addEventListener('change',()=>{
    const id=input.dataset.defaultChoice;markers();void act(()=>saveCard(id,true));
  }));
  window.addEventListener('languagechange', () => {markers();if(builtin) builtinSummary(builtin);});
  return { async load() {
    if (loaded) return;
    await act(async () => {
      const result = await api('/api/agents/settings'); saved = result.settings;
      descriptors = Object.fromEntries((result.backends || []).map(item => [item.id, {...item,executable:saved[`${item.id}_executable`] || agents[item.id]?.executable}]));
      try { $('agent-runtime-host').textContent=(await api('/api/environments')).agent_host; }
      catch { $('agent-runtime-host').textContent=t('无法读取运行位置'); }
      for (const id of Object.keys(agents)) {
        $(`${id}-executable`).value = saved[`${id}_executable`] || agents[id].executable;
        const cached=readConnectionCheck(checkStorage,id,$(`${id}-executable`).value);
        if(cached && descriptors[id]?.available) {probes[id]=cached;catalogs[id]=cached.models;}
        else clearConnectionCheck(checkStorage,id);
        modelOptions(id, saved[`${id}_model`] || '', saved[`${id}_effort`] || '');
      }
      loaded = true;
    });
  }};
}
