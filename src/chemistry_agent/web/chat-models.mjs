import { createCascadeMenu, effortLabel, permissionLabel } from './agent-menu.mjs';
import {createGroupPicker} from './group-picker.mjs';
import {t} from './i18n.mjs';
import {createModelPickerPanel} from './model-picker-panel.mjs';
import {createChatLocationPicker} from './chat-location.mjs';

const permissionIcons = {user:'hand', auto_review:'automation', full:'alert'};

export function createChatModelPicker({api, state, toast, updateControls, refreshHealth, openSettingsTab, onLocation=()=>{}}) {
  const $ = id => document.getElementById(id);
  const picker = $('agent-picker'), permission = $('permission-picker');
  const permissions = createCascadeMenu(permission, 'permission-menu');
  let group = [];
  const groupBody = members => JSON.stringify({strategy: members[0]?.strategy || 'ordered', members: members.map(({id, version}) => ({id, version}))});
  const groupMenu = createGroupPicker(picker, {api, current:()=>group, settings:()=>openSettings(), save:async members=>{
    const version = generation, session = state.sessionId;
    saving = true; updateControls();
    try {
      const result = session ? await api(`/api/agents/sessions/${session}/group`, {method:'PUT', body:groupBody(members)}) : {members};
      if (generation !== version || state.sessionId !== session) return false;
      group = result.members;
      render();
      return true;
    } finally {saving = false; updateControls();}
  }});
  const menu = createModelPickerPanel(picker, {
    getAgents: () => locations.agents.filter(agent => agent.enabled && agent.host_id === (selection.host_id || 'local')),
    getSelection: () => selection, agentChoice, catalog,
    choose: next => choose(next, {keepPickerOpen:true}), openSettings, openGroup:()=>groupMenu.open(),
  });
  let selection = null, defaultInstance = null, generation = 0, saving = false, catalogs = {};
  let locations={hosts:[],agents:[]};
  function openSettings(tab='agent', host=selection?.host_id) {
    openSettingsTab(tab, host);
  }
  const locationPicker=createChatLocationPicker({api,current:()=>selection,locations:()=>locations,loadLocations,choose,
    ensureSelection:async()=>{if(!selection)await defaults();},refreshModels:()=>refresh(false),
    openSettings});
  const instanceKey=value=>value?.instance_id || `local-${value?.backend || 'builtin'}`;
  const names = {builtin:'Chem Agent', codex:'Codex', antigravity:'Antigravity'};
  function render() {
    const current = selection || {backend:state.health?.agent_backend || 'builtin', model:state.health?.model || '', effort:'', permission:'user'};
    const models = catalogs[instanceKey(current)]?.models || [];
    const model = models.find(item => item.id === current.model)?.name || current.model || models.find(item => item.default)?.name || names[current.backend];
    const agent = locations.agents.find(item => item.id === instanceKey(current))?.name || names[current.backend];
    const modelDetails = [agent, model !== agent && model, current.effort && t(effortLabel(current.effort))].filter(Boolean);
    picker.dataset.grouped = String(Boolean(group.length));
    picker.setAttribute('aria-controls',group.length ? 'conversation-group-panel' : 'conversation-model-panel');
    picker.title = group.length ? group.map(item=>item.name).join(group[0].strategy === 'handoff' ? ' · ' : ' → ') : [t('Agent、模型与推理强度'), ...modelDetails].join(' · ');
    picker.setAttribute('aria-label', picker.title);
    $('composer-model').textContent = group.length ? t('群组') + ' · ' + group.length : model;
    const effort = $('composer-effort');
    effort.textContent = current.effort ? t(effortLabel(current.effort)) : '';
    effort.hidden = group.length > 0 || !current.effort;
    const logo = current.backend === 'antigravity'
      ? '<img class="icon agent-logo" src="/assets/antigravity.svg" alt="">'
      : `<svg class="icon agent-logo" aria-hidden="true"><use href="/assets/ui-icons.svg#i-${current.backend === 'codex' ? 'gpt' : 'model'}"/></svg>`;
    const logoElement = picker.querySelector('.agent-logo');
    if (picker.dataset.backend !== current.backend) {
      logoElement.outerHTML = logo;
      picker.dataset.backend = current.backend;
    }
    permission.title = `${t('执行权限')} · ${t(permissionLabel(current.permission))}`;
    permission.setAttribute('aria-label', permission.title);
    $('composer-permission').textContent = t(permissionLabel(current.permission));
    permission.querySelector('use').setAttribute('href', `/assets/ui-icons.svg#i-${permissionIcons[current.permission] || 'hand'}`);
    if(state.health) {
      $('model-status').classList.toggle('unavailable',!group.length && !state.health.model_configured);
      $('model-label').textContent=state.health.model_configured?`${model} · ${t('已配置')}`:t('模型未配置');
    }
    locationPicker.render();
    onLocation(selection);
    updateControls();
  }
  async function choose(next, {keepPickerOpen=false, keepLocationOpen=false}={}) {
    const version = generation, id = state.sessionId;
    if (saving) return false;
    saving = true;
    if (!keepPickerOpen) menu.close();
    permissions.close();
    if (!keepLocationOpen) locationPicker.close();
    render();
    try {
      const result = id ? await api(`/api/agents/sessions/${id}`, {method:'PUT', body:JSON.stringify(next)}) : next;
      if (version !== generation) return false;
      selection = result;
      if (id) await refreshHealth();
      else {
        const catalog = catalogs[instanceKey(next)];
        state.health = {...state.health, agent_backend:next.backend, model:next.model,
          provider:next.backend !== 'builtin' ? next.backend : (catalog?.provider || 'chatgpt'),
          model_configured:next.backend !== 'builtin' ? Boolean(catalog?.authenticated) : (catalog?.configured ?? state.health?.model_configured)};
      }
      return version === generation;
    } catch (error) {
      if (version !== generation) return false;
      if (keepLocationOpen) throw error;
      toast(error.message);
      return false;
    } finally { saving = false; render(); }
  }
  const catalogRequests = new Map();
  async function catalog(instance, reload = false) {
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
  async function loadLocations() {locations=await api('/api/hosting');}
  function agentChoice(instance) {
    return {...selection,instance_id:instance.id,host_id:instance.host_id,backend:instance.backend,
      directory:selection.host_id===instance.host_id?selection.directory:'',model:instance.model||'',effort:instance.effort||'',
      permission:instance.backend==='builtin' && selection.permission==='auto_review'?'user':selection.permission,monitor_host:selection.monitor_host||'auto'};
  }
  picker.addEventListener('click', async () => {
    if (groupMenu.visible) {groupMenu.close(); return;}
    if (group.length) {void groupMenu.open(); return;}
    if (menu.visible) { menu.close(); return; }
    if (!selection) await defaults();
    if (selection) menu.open();
  });
  permission.addEventListener('click', () => {
    if (permissions.visible) { permissions.close(); return; }
    if (!selection) return;
    permissions.open('执行权限', Object.keys(permissionIcons).map(value => ({label:permissionLabel(value), selected:selection.permission===value,
      icon:permissionIcons[value], warning:value==='full',
      disabled:selection.backend==='builtin' && value==='auto_review', title:selection.backend==='builtin' && value==='auto_review' ? '此 Agent 暂不支持自动审批' : '', action:() => choose({...selection, permission:value})})));
  });
  async function defaults() {
    const version = ++generation;
    try { const [data] = await Promise.all([api('/api/agents/selection'),loadLocations()]); if (version === generation) { selection = data; defaultInstance = instanceKey(data); render(); } }
    catch(error) { if(version === generation) toast(error.message); }
  }
  async function refresh(reload = true) {
    if (!selection) await defaults();
    if (!selection) return;
    const value=selection;
    try {
      const data=await catalog(instanceKey(value), reload);
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
        const card=document.createElement('article');card.className='agent-approval';card.dataset.approvalId=item.id;
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
  return {refreshApprovals:approvals,get grouped(){return group.length > 0;},get mentionable(){return group[0]?.strategy === 'handoff' ? group : [];},get value(){return selection?.backend==='builtin' ? selection.model || null : null;},get saving(){return saving;},render,refresh,
    adaptHealth(health){
      if(state.sessionId || !selection) return health;
      const data=catalogs[instanceKey(selection)];
      const matchesDefault=instanceKey(selection)===defaultInstance && selection.backend===health.agent_backend;
      const configured=selection.backend!=='builtin'?data?.authenticated:data?.configured;
      return {...health, agent_backend:selection.backend, model:selection.model,
        provider:selection.backend!=='builtin'?selection.backend:data?.provider || (matchesDefault?health.provider:'chatgpt'),
        model_configured:configured ?? (matchesDefault && health.model_configured)};
    },
    reset(){group=[];groupMenu.close();generation++;selection=null;defaultInstance=null;catalogs={};catalogRequests.clear();menu.close();permissions.close();locationPicker.close();render();},
    newConversation(){group=[];groupMenu.close();selection=null;defaultInstance=null;menu.close();permissions.close();locationPicker.close();void defaults();},
    async loadSession(id){group=[];groupMenu.close();const version=++generation;menu.close();permissions.close();locationPicker.close();const [data]=await Promise.all([api(`/api/agents/sessions/${id}`),loadLocations()]);if(version===generation){selection=data;group=data.group || [];render();}},
    async saveNewSession(id){if(selection) await api(`/api/agents/sessions/${id}`,{method:'PUT',body:JSON.stringify(selection)});if(group.length) await api(`/api/agents/sessions/${id}/group`, {method:'PUT',body:groupBody(group)});},
  };
}
