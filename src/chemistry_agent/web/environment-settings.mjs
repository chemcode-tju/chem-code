import {t, translateUI} from './i18n.mjs';
import {esc, facts} from './compute-settings-api.mjs';
import {readConnectionCheck, saveConnectionCheck, clearConnectionCheck} from './connection-checks.mjs';

export const connectionNames = {local:'本机直连', runner:'HTTP Runner', ssh:'SSH 主机'};
export const environmentIdentity = row => JSON.stringify([row.id,row.transport,row.endpoint,row.ssh_alias,row.ssh,row.has_token]);

export function createEnvironmentSettings(api) {
  const $ = id => document.getElementById(id);
  const focusNode = (action,id) => $('node-list').querySelector(`[data-${action}="${id}"]`)?.focus();
  let inventory = {environments:[],ssh_choices:[]}, busy = false, editing = null, dirty = false, removing = null;
  let storage, noticeTimer;
  try { storage = window.sessionStorage; } catch {}
  const notify = (message='', error=false) => {
    clearTimeout(noticeTimer); $('node-notice').textContent = t(message);
    $('node-notice').classList.toggle('error',error);
    if (message) noticeTimer=setTimeout(()=>{$('node-notice').textContent='';},error?8000:5000);
  };
  function controls() {
    $('node-list').setAttribute('aria-busy',String(busy));
    document.querySelectorAll('#servers button,#servers input,#servers select').forEach(el => {
      el.disabled = busy || (Boolean(el.dataset.probeNode) && !inventory.environments.find(row=>row.id===el.dataset.probeNode)?.enabled);
    });
  }
  function attachEditor() {
    const form=$('node-form');
    if (editing === null) { form.hidden=true; return; }
    const card=[...$('node-list').children].find(row=>row.dataset.nodeId===editing);
    (card || $('node-editor-home')).append(form); form.hidden=false;
  }
  function render() {
    $('node-editor-home').append($('node-form'));
    const expanded=new Set([...$('node-list').querySelectorAll('.server-source[open]')].map(detail=>detail.closest('.server-card').dataset.nodeId));
    $('node-list').innerHTML=inventory.environments.map(row=>{
      const check=row.enabled ? readConnectionCheck(storage,'node:'+row.id,environmentIdentity(row)) : null;
      const state=!row.enabled ? '未启用' : check ? (check.authenticated?'上次测试通过':'上次测试失败') : '尚未测试连接';
      const date=check ? new Date(check.checkedAt).toLocaleString(document.documentElement.lang) : '';
      const address=row.transport==='runner' ? row.endpoint : row.transport==='local' ? inventory.platform_host
        : [row.ssh?.user,row.ssh?.hostname || row.ssh_alias || row.id].filter(Boolean).join('@');
      return `<article class="server-card" data-node-id="${esc(row.id)}"><header>
        <label class="server-activation"><input type="checkbox" role="switch" class="settings-switch" data-enable-node="${esc(row.id)}" aria-label="${esc(t('允许 Agent 使用')+' '+row.name)}" ${row.enabled?'checked':''}></label>
        <div class="server-identity"><strong data-no-i18n>${esc(row.name)}</strong><div class="server-subtitle"><span>${esc(t(connectionNames[row.transport]))}</span>${row.capabilities?.length?`<span data-no-i18n>· ${esc(row.capabilities.join(' / '))}</span>`:''}</div><span class="server-check-state" data-state="${check?(check.authenticated?'passed':'failed'):'unknown'}" title="${esc(date)}">${esc(t(state))}</span></div>
        <div class="server-card-actions"><button type="button" class="secondary" data-probe-node="${esc(row.id)}">${esc(t('测试连接'))}</button><button type="button" class="icon-button toolbar-button" data-edit-node="${esc(row.id)}" aria-label="${esc(t('编辑节点')+' · '+row.name)}" title="${esc(t('编辑节点'))}"><svg class="icon" aria-hidden="true"><use href="/assets/ui-icons.svg#i-edit"/></svg></button></div></header>
        <details class="server-source" ${expanded.has(row.id)?'open':''}><summary>${esc(t('连接信息'))}</summary>${facts([['节点标识',row.id],['地址',address],['SSH 主机',row.transport==='ssh' ? row.ssh_alias || row.id : ''],['配置文件',row.ssh?.source],['测试时间',date]])}<button type="button" class="quiet-button remove-node" data-remove-node="${esc(row.id)}">${esc(t('移除节点'))}</button></details>
        ${removing===row.id?`<div class="node-removal"><p>${esc(t('移除连接配置？任务与文件保留。'))}</p><div class="actions"><button type="button" class="quiet-button" data-cancel-remove>${esc(t('取消'))}</button><button type="button" class="secondary remove-node" data-confirm-remove="${esc(row.id)}">${esc(t('移除'))}</button></div></div>`:''}</article>`;
    }).join('') || `<p class="server-empty">${esc(t('尚未添加节点'))}</p>`;
    attachEditor(); controls(); translateUI();
  }
  function choices(selected='') {
    const used=new Set(inventory.environments.filter(row=>row.id!==editing).flatMap(row=>[row.id,row.ssh_alias].filter(Boolean)));
    const options=inventory.ssh_choices.filter(row=>!used.has(row.alias));
    if(selected && !options.some(row=>row.alias===selected)) options.push({alias:selected});
    $('node-ssh-alias').replaceChildren(new Option(t('选择服务器'),''),...options.map(row=>new Option(row.alias,row.alias)));
    $('node-ssh-alias').value=selected;
  }
  function fields() {
    const mode=$('node-transport').value;
    $('node-ssh-fields').hidden=mode!=='ssh'; $('node-runner-fields').hidden=mode!=='runner';
    $('node-ssh-alias').required=mode==='ssh'; $('node-endpoint').required=mode==='runner';
  }
  function edit(row=null) {
    if(editing!==null && dirty) {notify(t('请先保存或取消当前修改。'),true);return;}
    editing=row?.id || ''; dirty=false; removing=null;
    $('node-editor-title').textContent=t(row?'编辑节点':'添加节点');
    $('node-id').value=row?.id || ''; $('node-id').readOnly=Boolean(row);
    $('node-name').value=row?.name || ''; $('node-transport').value=row?.transport || 'ssh';
    $('node-endpoint').value=row?.endpoint || 'http://127.0.0.1:8767';
    $('node-token').value=''; $('node-token').placeholder=t(row?.has_token?'已保存（留空保留）':'连接令牌');
    $('node-capabilities').value=(row?.capabilities || []).join(', '); $('node-enabled').checked=row?.enabled!==false;
    choices(row?.ssh_alias || (row?.transport==='ssh'?row.id:'')); fields(); render(); $('node-transport').focus();
  }
  async function refresh() {
    inventory=await api('/api/environments'); render();
    document.dispatchEvent(new Event('chem:environments-updated'));
  }
  async function act(operation) {
    if(busy)return; busy=true;controls();notify();
    try {await operation();} catch(error) {notify(error.message,true);render();}
    finally {busy=false;controls();}
  }
  $('node-new').onclick=()=>edit();
  $('node-cancel').onclick=()=>{const id=editing;editing=null;dirty=false;render();if(id)focusNode('edit-node',id);else $('node-new').focus();};
  $('node-form').oninput=()=>{dirty=true;}; $('node-form').onchange=()=>{dirty=true;};
  $('node-endpoint').oninput=()=>{
    const record=inventory.environments.find(row=>row.id===editing);
    $('node-token').placeholder=t(record?.has_token && record.endpoint===$('node-endpoint').value.trim().replace(/\/$/,'')?'已保存（留空保留）':'连接令牌');
  };
  $('node-transport').onchange=()=>{
    fields();
    if(editing==='' && $('node-transport').value==='local') {
      $('node-id').value='local'; $('node-name').value=inventory.platform_host || 'local';
    }
  };
  $('node-ssh-alias').onchange=()=>{
    if(editing==='') {$('node-id').value=$('node-ssh-alias').value;$('node-name').value=$('node-ssh-alias').value;}
  };
  $('node-refresh').onclick=()=>act(async()=>{
    const selected=$('node-ssh-alias').value;await refresh();choices(selected);
  });
  $('node-form').onsubmit=event=>{
    event.preventDefault();act(async()=>{
      const id=$('node-id').value.trim(), record=inventory.environments.find(row=>row.id===editing), transport=$('node-transport').value;
      const body={id,name:$('node-name').value.trim(),transport,
        ssh_alias:transport==='ssh'?$('node-ssh-alias').value:record?.ssh_alias || null,
        endpoint:$('node-endpoint').value.trim(),token:$('node-token').value || null,
        enabled:$('node-enabled').checked,
        capabilities:$('node-capabilities').value.split(',').map(value=>value.trim()).filter(Boolean)};
      await api(record?`/api/environments/${encodeURIComponent(id)}`:'/api/environments',{method:record?'PUT':'POST',body:JSON.stringify(body)});
      clearConnectionCheck(storage,'node:'+id);editing=null;dirty=false;await refresh();notify(t('已保存'));
    }).finally(()=>{if(editing===null)focusNode('edit-node',$('node-id').value.trim());});
  };
  $('node-list').onchange=event=>{
    const input=event.target.closest('[data-enable-node]');if(!input)return;
    const enabled=input.checked;
    act(async()=>{
      await api(`/api/environments/${input.dataset.enableNode}`,{method:'PATCH',body:JSON.stringify({enabled})});
      clearConnectionCheck(storage,'node:'+input.dataset.enableNode);await refresh();
      if(!enabled)notify(t('已停用此服务器；正在运行的计算不受影响'));
    }).finally(()=>focusNode('enable-node',input.dataset.enableNode));
  };
  $('node-list').onclick=event=>{
    const button=event.target.closest('button');if(!button)return;
    if(button.dataset.editNode) {edit(inventory.environments.find(row=>row.id===button.dataset.editNode));return;}
    if(button.dataset.removeNode) {removing=button.dataset.removeNode;render();focusNode('confirm-remove',removing);return;}
    if(button.hasAttribute('data-cancel-remove')) {const id=removing;removing=null;render();focusNode('remove-node',id);return;}
    if(button.dataset.confirmRemove) {act(async()=>{
      const id=button.dataset.confirmRemove;await api(`/api/environments/${id}`,{method:'DELETE'});
      clearConnectionCheck(storage,'node:'+id);if(editing===id){editing=null;dirty=false;}
      removing=null;await refresh();notify(t('已移除节点'));
    }).finally(()=>{if(removing===null)$('node-new').focus();});return;}
    if(button.dataset.probeNode)act(async()=>{
      const row=inventory.environments.find(row=>row.id===button.dataset.probeNode);notify(t('正在检测…'));
      try {
        const result=await api(`/api/environments/${row.id}/probe`,{method:'POST'});
        saveConnectionCheck(storage,'node:'+row.id,environmentIdentity(row),{authenticated:result.ok===true});
        notify(t(result.ok?'连接成功':'上次测试失败'),!result.ok);
      } catch(error) {
        saveConnectionCheck(storage,'node:'+row.id,environmentIdentity(row),{authenticated:false});throw error;
      } finally {render();}
    }).finally(()=>focusNode('probe-node',button.dataset.probeNode));
  };
  window.addEventListener('languagechange',()=>render());
  return {async activate(tab) {if(tab==='servers' && !busy) await act(refresh);}};
}
