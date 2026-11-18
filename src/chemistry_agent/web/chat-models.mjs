import { createCascadeMenu, effortLabel, permissionLabel } from './agent-menu.mjs';
import {t} from './i18n.mjs';
import {createChatLocationPicker} from './chat-location.mjs';

export function createChatModelPicker({api, state, toast, updateControls, refreshHealth}) {
  const $ = id => document.getElementById(id);
  const picker = $('agent-picker'), permission = $('permission-picker');
  const menu = createCascadeMenu(picker), permissions = createCascadeMenu(permission);
  let selection = null, generation = 0, saving = false, catalogs = {}, browsing = 0;
  let locations={hosts:[],agents:[]};
  const locationPicker=createChatLocationPicker({api,current:()=>selection,locations:()=>locations,loadLocations,choose,
    ensureSelection:async()=>{if(!selection)await defaults();},refreshModels:()=>refresh(false),
    openSettings:(tab='agent')=>{const link=$('model-status'),href=link.href;link.href=`/settings#${tab}`;link.click();link.href=href;}});
  const instanceKey=value=>value?.instance_id || `local-${value?.backend || 'builtin'}`;
  const locationName=id=>id==='local'?t('本机'):(locations.hosts.find(h=>h.id===id)?.name || id);
  const names = {builtin:'Chem Agent', codex:'Codex', antigravity:'Antigravity'};
  function render() {
    const current = selection || {backend:state.health?.agent_backend || 'builtin', model:state.health?.model || '', effort:'', permission:'user'};
    const models = catalogs[instanceKey(current)]?.models || [];
    const model = models.find(item => item.id === current.model)?.name || current.model || models.find(item => item.default)?.name || names[current.backend];
    $('agent-picker-label').textContent = `${model}${current.effort ? " · " + t(effortLabel(current.effort)) : ""}`;
    $('agent-picker-label').dataset.noI18n = '';
    picker.querySelector('.agent-logo').outerHTML = current.backend === 'antigravity'
      ? '<img class="icon agent-logo" src="/assets/antigravity.svg" alt="">'
      : '<svg class="icon agent-logo" aria-hidden="true"><use href="/assets/ui-icons.svg#i-gpt"/></svg>';
    picker.title = `${names[current.backend]} · ${model} · ${t(effortLabel(current.effort))}`;
    $('permission-label').textContent = permissionLabel(current.permission);
    permission.title = current.backend !== 'builtin' ? `${locationName(current.host_id || 'local')} · ${names[current.backend]} 执行权限` : 'Chem Agent 工具执行权限';
    if(state.health) {
      $('model-status').classList.toggle('unavailable',!state.health.model_configured);
      $('model-label').textContent=state.health.model_configured?`${model} · ${t('已配置')}`:t('模型未配置');
    }
    locationPicker.render();
    updateControls();
  }
  async function choose(next) {
    const version = generation, id = state.sessionId;
    saving = true; render(); menu.close(); permissions.close(); locationPicker.close();
    try {
      const result = id ? await api(`/api/agents/sessions/${id}`, {method:'PUT', body:JSON.stringify(next)}) : next;
      if (version !== generation) return;
      selection = result;
      if (id) await refreshHealth();
      else {
        const catalog = catalogs[instanceKey(next)];
        state.health = {...state.health, agent_backend:next.backend, model:next.model,
          provider:next.backend !== 'builtin' ? next.backend : (catalog?.provider || 'chatgpt'),
          model_configured:next.backend !== 'builtin' ? Boolean(catalog?.authenticated) : (catalog?.configured ?? state.health?.model_configured)};
      }
    } catch (error) { if (version === generation) toast(error.message); }
    finally { saving = false; render(); }
  }
  const catalogRequests = new Map();
  async function catalog(backend, reload = false, instance = instanceKey(selection)) {
    if (catalogs[instance] && !reload) return catalogs[instance];
    if (!catalogRequests.has(instance)) {
      const version = state.accountVersion;
      const request = api(`/api/hosting/agents/${encodeURIComponent(instance)}/models`).then(data => {
        if (version === state.accountVersion) catalogs[instance] = data;
        return data;
      }).finally(() => {if (catalogRequests.get(instance) === request) catalogRequests.delete(instance);});
      catalogRequests.set(instance, request);
    }
    return catalogRequests.get(instance);
  }
  function currentModel() {
    const rows = catalogs[instanceKey(selection)]?.models || [];
    return rows.find(model => model.id === selection?.model) || (!selection?.model ? rows.find(model => model.default) : null);
  }
  function overview() {
    const model = currentModel();
    menu.open('模型选择', [
      {label:'Agent', value:locations.agents.find(a=>a.id===instanceKey(selection))?.name || names[selection.backend], valueNoTranslate:true, branch:true, action:agents},
      {label:'模型', value:model?.name || selection.model || t('跟随默认模型'), valueNoTranslate:true, branch:true, action:models},
      {label:'推理', value:t(effortLabel(selection.effort)), branch:true, action:efforts},
    ]);
  }
  async function loadLocations() {locations=await api('/api/hosting');}
  function agentChoice(instance) {
    return {...selection,instance_id:instance.id,host_id:instance.host_id,backend:instance.backend,
      directory:selection.host_id===instance.host_id?selection.directory:'',model:instance.model||'',effort:instance.effort||'',
      permission:instance.backend==='builtin' && selection.permission==='auto_review'?'user':selection.permission,monitor_host:selection.monitor_host||'auto'};
  }
  async function agents(host=selection.host_id || 'local',depth=1) {
    browsing++;
    if(!locations.hosts.length)await loadLocations();
    const rows=locations.agents.filter(a=>a.host_id===host&&a.enabled).map(instance=>({label:instance.name,noTranslate:true,branch:true,
      selected:instanceKey(selection)===instance.id,action:()=>instanceModels(instance,depth+1)}));
    if(!rows.length)rows.push({message:'此主机尚未添加 Agent'});
    rows.push({label:'设置…',action:()=>{menu.close();$('model-status').click();}});menu.column(depth,'Agent',rows);
  }
  async function instanceModels(instance,depth) {
    const version=++browsing;menu.column(depth,'模型',[{message:'正在读取模型…'}]);
    try {
      const data=await catalog(instance.backend,false,instance.id);
      if(version!==browsing||!menu.isColumnOpen(depth,'模型'))return;
      const base=instanceKey(selection)===instance.id?selection:agentChoice(instance);
      const rows=(data.models||[]).map(model=>({label:model.name||model.id,noTranslate:true,searchText:model.id,
        selected:instanceKey(selection)===instance.id&&selection.model===model.id,
        action:()=>choose({...base,model:model.id,effort:model.efforts?.includes(base.effort)?base.effort:''})}));
      rows.push({label:'跟随默认模型',action:()=>choose(base)});
      if(data.message&&!data.authenticated&&instance.backend!=='builtin')rows.push({message:data.message});
      menu.column(depth,'模型',rows,null,t('搜索模型'));
    } catch(error){menu.column(depth,'模型',[{message:error.message},{label:'使用已保存配置',action:()=>choose(agentChoice(instance))}]);}
  }
  async function efforts() {
    const version = ++browsing, backend = selection.backend;
    menu.column(1, '推理强度', [{message:'正在读取模型…'}]);
    let error;
    try {await catalog(backend);} catch (failure) {error=failure.message;}
    if (version !== browsing || !menu.isColumnOpen(1, '推理强度')) return;
    const model=currentModel(), values=[...new Set(['',...(model?.efforts || []).filter(Boolean)])];
    const rows=values.map(effort=>({label:effortLabel(effort),selected:selection.effort === effort,
      action:()=>choose({...selection,effort})}));
    if(error) rows.push({message:error});
    menu.column(1, '推理强度', rows);
  }
  function modelRow(model) {
    return {label:model.name || model.id, noTranslate:true, searchText:[model.name,model.id].filter(Boolean).join(' '),
      selected:selection.model === model.id, action:() => choose({...selection, model:model.id,
        effort:model.efforts?.includes(selection.effort) ? selection.effort : ''})};
  }
  async function models(reload = false) {
    const version = ++browsing, backend = selection.backend;
    menu.column(1, '模型', [{message:'正在读取模型…'}]);
    let data, error;
    try { data = await catalog(backend, reload); } catch (failure) { error = failure.message; }
    if (version !== browsing || !menu.isColumnOpen(1, '模型')) return;
    const rows = (data?.models || []).map(modelRow);
    if (error) rows.push({message:error});
    if (data?.message && !data.authenticated && backend !== 'builtin') rows.push({message:data.message});
    if (selection.model && !data?.models?.some(m => m.id === selection.model)) rows.unshift(modelRow({id:selection.model}));
    rows.push({label:'跟随默认模型', selected:!selection.model, action:() => choose({...selection,model:'',effort:''})});
    rows.push({label:'刷新模型', action:() => models(true)}, {label:'设置…', action:() => { menu.close(); $('model-status').click(); }});
    const manual = backend === 'builtin' && data?.provider !== 'chatgpt' ? () => {
      const form = document.createElement('div'); form.className='menu-manual';
      const input = document.createElement('input'); input.placeholder='模型 ID'; input.maxLength=160; input.setAttribute('aria-label','模型 ID');
      const apply = document.createElement('button'); apply.type='button'; apply.textContent='应用'; apply.onclick=() => { if(input.value.trim()) modelRow({id:input.value.trim()}).action(); };
      input.onkeydown=event => { if(event.key==='Enter') {event.preventDefault();apply.click();} }; form.append(input, apply); return form;
    } : null;
    menu.column(1, '模型', rows, manual, t('搜索模型'));
  }
  picker.addEventListener('click', async () => {
    if (menu.visible) { menu.close(); return; }
    if (!selection) await defaults();
    if (selection) overview();
  });
  permission.addEventListener('click', () => {
    if (permissions.visible) { permissions.close(); return; }
    if (!selection) return;
    permissions.open('执行权限', ['user','auto_review','full'].map(value => ({label:permissionLabel(value), selected:selection.permission===value,
      disabled:selection.backend==='builtin' && value==='auto_review', title:selection.backend==='builtin' && value==='auto_review' ? '此 Agent 暂不支持自动审批' : '', action:() => choose({...selection, permission:value})})));
  });
  async function defaults() {
    const version = ++generation;
    try { const [data] = await Promise.all([api('/api/agents/selection'),loadLocations()]); if (version === generation) { selection = data; render(); } }
    catch(error) { if(version === generation) toast(error.message); }
  }
  async function refresh(reload = true) {
    if (!selection) await defaults();
    if (!selection) return;
    const value=selection;
    try {
      const data=await catalog(value.backend, reload);
      if(selection!==value)return;
      if(!state.sessionId)state.health={...state.health,agent_backend:value.backend,model:value.model,
        provider:value.backend==='builtin'?data.provider:value.backend,
        model_configured:value.backend==='builtin'?Boolean(data.configured):Boolean(data.authenticated)};
      render();
    } catch { /* Catalog failure is shown when opened, not on every health tick. */ }
  }
  let approvalSession, approvalBusy = false, approvalSignature = '';
  async function approvals() {
    if (approvalBusy) return;
    const id = state.sessionId, container = $('agent-approvals');
    if (approvalSession !== id) { approvalSession=id; approvalSignature=''; container.replaceChildren(); container.hidden=true; }
    if (!id || document.hidden) return;
    approvalBusy=true;
    try {
      const data=await api(`/api/agents/sessions/${id}/approvals`);
      if (state.sessionId!==id) return;
      const signature=JSON.stringify(data.items); if(signature===approvalSignature) return; approvalSignature=signature;
      container.replaceChildren(); container.hidden=!data.items.length;
      for(const item of data.items) {
        const card=document.createElement('article');card.className='agent-approval';
        const title=document.createElement('header');title.textContent=item.title;
        const details=document.createElement('details'), summary=document.createElement('summary'), pre=document.createElement('pre');summary.textContent='查看操作';pre.textContent=JSON.stringify(item.details,null,2);details.append(summary,pre);
        const actions=document.createElement('div');actions.className='approval-actions';
        for(const [label,approved] of [['拒绝',false],['批准',true]]) {const button=document.createElement('button');button.type='button';button.textContent=label;button.onclick=async()=>{
          actions.querySelectorAll('button').forEach(node=>node.disabled=true);
          try {await api(`/api/agents/sessions/${id}/approvals/${item.id}`,{method:'POST',body:JSON.stringify({approved})});approvalSignature='';await approvals();}
          catch(error){toast(error.message);actions.querySelectorAll('button').forEach(node=>node.disabled=false);}
        }; actions.append(button);}
        card.append(title,details,actions);container.append(card);
      }
    } catch { /* Ordinary run polling reports connection errors. */ }
    finally { approvalBusy=false; }
  }
  setInterval(approvals,2000);
  window.addEventListener('languagechange',render);
  return {get value(){return selection?.backend==='builtin' ? selection.model || null : null;},get saving(){return saving;},render,refresh,
    adaptHealth(health){
      if(state.sessionId || !selection) return health;
      const data=catalogs[instanceKey(selection)];
      return {...health, agent_backend:selection.backend, model:selection.model,
        provider:selection.backend!=='builtin'?selection.backend:data?.provider || 'chatgpt',
        model_configured:selection.backend!=='builtin'?Boolean(data?.authenticated):Boolean(data?.configured)};
    },
    reset(){generation++;selection=null;catalogs={};catalogRequests.clear();menu.close();permissions.close();locationPicker.close();render();},
    newConversation(){selection=null;menu.close();permissions.close();locationPicker.close();void defaults();},
    async loadSession(id){const version=++generation;menu.close();permissions.close();locationPicker.close();const [data]=await Promise.all([api(`/api/agents/sessions/${id}`),loadLocations()]);if(version===generation){selection=data;render();}},
    async saveNewSession(id){if(selection) await api(`/api/agents/sessions/${id}`,{method:'PUT',body:JSON.stringify(selection)});},
  };
}
