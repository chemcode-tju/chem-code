import { createCascadeMenu, effortLabel, permissionLabel } from './agent-menu.mjs';

export function createChatModelPicker({api, state, toast, updateControls, refreshHealth}) {
  const $ = id => document.getElementById(id);
  const picker = $('agent-picker'), permission = $('permission-picker');
  const menu = createCascadeMenu(picker), permissions = createCascadeMenu(permission);
  let selection = null, generation = 0, saving = false, catalogs = {}, browsing = 0;
  const names = {builtin:'Chem Agent', codex:'Codex', antigravity:'Antigravity'};
  function render() {
    const current = selection || {backend:state.health?.agent_backend || 'builtin', model:state.health?.model || '', effort:'', permission:'user'};
    const models = catalogs[current.backend]?.models || [];
    const model = models.find(item => item.id === current.model)?.name || current.model || models.find(item => item.default)?.name || names[current.backend];
    $('agent-picker-label').textContent = `${model} · ${effortLabel(current.effort)}`;
    picker.querySelector('.agent-logo').outerHTML = current.backend === 'antigravity'
      ? '<img class="icon agent-logo" src="/assets/antigravity.svg" alt="">'
      : '<svg class="icon agent-logo" aria-hidden="true"><use href="/assets/ui-icons.svg#i-gpt"/></svg>';
    picker.title = `${names[current.backend]} · ${model} · ${effortLabel(current.effort)}`;
    $('permission-label').textContent = permissionLabel(current.permission);
    permission.title = current.backend !== 'builtin' ? `${names[current.backend]} 本机执行权限；平台工具沿用平台授权` : 'Chem Agent 工具执行权限';
    updateControls();
  }
  async function choose(next) {
    const version = generation, id = state.sessionId;
    saving = true; render(); menu.close(); permissions.close();
    try {
      const result = id ? await api(`/api/agents/sessions/${id}`, {method:'PUT', body:JSON.stringify(next)}) : next;
      if (version !== generation) return;
      selection = result;
      if (id) await refreshHealth();
      else {
        const catalog = catalogs[next.backend];
        state.health = {...state.health, agent_backend:next.backend, model:next.model,
          provider:next.backend !== 'builtin' ? next.backend : (catalog?.provider || 'chatgpt'),
          model_configured:next.backend !== 'builtin' ? Boolean(catalog?.authenticated) : (catalog?.configured ?? state.health?.model_configured)};
      }
    } catch (error) { if (version === generation) toast(error.message); }
    finally { saving = false; render(); }
  }
  const catalogRequests = new Map();
  async function catalog(backend, reload = false) {
    if (catalogs[backend] && !reload) return catalogs[backend];
    if (!catalogRequests.has(backend)) {
      const version = state.accountVersion;
      const request = api(`/api/agents/catalog/${backend}`).then(data => {
        if (version === state.accountVersion) catalogs[backend] = data;
        return data;
      }).finally(() => {if (catalogRequests.get(backend) === request) catalogRequests.delete(backend);});
      catalogRequests.set(backend, request);
    }
    return catalogRequests.get(backend);
  }
  function effortRows(backend, model) {
    const next = {backend, model:model.id, permission:selection?.backend === backend ? selection.permission : backend !== 'builtin' ? 'user' : 'full'};
    const values = ['', ...(model.efforts || []).filter(Boolean)];
    menu.column(2, '推理强度', [...new Set(values)].map(effort => ({label:effortLabel(effort), selected:selection?.backend === backend && selection.model === model.id && selection.effort === effort, action:() => choose({...next, effort})})));
  }
  function modelRow(backend, model) {
    return {label:model.name || model.id, branch:Boolean(model.efforts?.length), selected:selection?.backend === backend && selection.model === model.id,
      action:() => model.efforts?.length ? effortRows(backend, model) : choose({backend, model:model.id, effort:'', permission:selection?.backend === backend ? selection.permission : backend !== 'builtin' ? 'user' : 'full'})};
  }
  async function models(backend, reload = false) {
    const version = ++browsing;
    menu.column(1, names[backend], [{message:'正在读取模型…'}]);
    let data, error;
    try { data = await catalog(backend, reload); } catch (failure) { error = failure.message; }
    if (version !== browsing || !menu.isColumnOpen(1, names[backend])) return;
    const rows = (data?.models || []).map(model => modelRow(backend, model));
    if (error) rows.push({message:error});
    if (data?.message && !data.authenticated && backend !== 'builtin') rows.push({message:data.message});
    if (selection?.backend === backend && selection.model && !data?.models?.some(m => m.id === selection.model)) rows.unshift(modelRow(backend, {id:selection.model}));
    rows.push({label:'跟随默认模型', action:() => choose({backend, model:'', effort:'', permission:backend !== 'builtin'?'user':'full'})});
    rows.push({label:'刷新模型', action:() => models(backend, true)}, {label:'设置…', action:() => { menu.close(); $('model-status').click(); }});
    const manual = backend === 'builtin' && data?.provider !== 'chatgpt' ? () => {
      const form = document.createElement('div'); form.className='menu-manual';
      const input = document.createElement('input'); input.placeholder='模型 ID'; input.maxLength=160; input.setAttribute('aria-label','模型 ID');
      const apply = document.createElement('button'); apply.type='button'; apply.textContent='应用'; apply.onclick=() => { if(input.value.trim()) modelRow(backend,{id:input.value.trim()}).action(); };
      input.onkeydown=event => { if(event.key==='Enter') {event.preventDefault();apply.click();} }; form.append(input, apply); return form;
    } : null;
    menu.column(1, names[backend], rows, manual);
  }
  picker.addEventListener('click', async () => {
    if (menu.visible) { menu.close(); return; }
    if (!selection) await defaults();
    menu.open('Agent', Object.entries(names).map(([backend,name]) => ({label:name, branch:true, active:selection?.backend===backend, action:() => models(backend)})));
  });
  permission.addEventListener('click', () => {
    if (permissions.visible) { permissions.close(); return; }
    if (!selection) return;
    permissions.open('执行权限', ['user','auto_review','full'].map(value => ({label:permissionLabel(value), selected:selection.permission===value,
      disabled:selection.backend==='builtin' && value==='auto_review', title:selection.backend==='builtin' && value==='auto_review' ? '此 Agent 暂不支持自动审批' : '', action:() => choose({...selection, permission:value})})));
  });
  async function defaults() {
    const version = ++generation;
    try { const data = await api('/api/agents/selection'); if (version === generation) { selection = data; render(); } }
    catch(error) { if(version === generation) toast(error.message); }
  }
  async function refresh(reload = true) {
    if (!selection) await defaults();
    if (!selection) return;
    try { await catalog(selection.backend, reload); render(); } catch { /* Catalog failure is shown when opened, not on every health tick. */ }
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
      if(state.sessionId || !selection || health.agent_backend===selection.backend) return health;
      const data=catalogs[selection.backend];
      return {...health, agent_backend:selection.backend, model:selection.model,
        provider:selection.backend!=='builtin'?selection.backend:data?.provider || 'chatgpt',
        model_configured:selection.backend!=='builtin'?Boolean(data?.authenticated):Boolean(data?.configured)};
    },
    reset(){generation++;selection=null;catalogs={};catalogRequests.clear();menu.close();permissions.close();render();},
    newConversation(){selection=null;menu.close();permissions.close();void defaults();},
    async loadSession(id){const version=++generation;menu.close();permissions.close();const data=await api(`/api/agents/sessions/${id}`);if(version===generation){selection=data;render();}},
    async saveNewSession(id){if(selection) await api(`/api/agents/sessions/${id}`,{method:'PUT',body:JSON.stringify(selection)});},
  };
}
