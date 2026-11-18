import {t, translateUI} from './i18n.mjs';

export function createHostingSettings(api, selectTab) {
  const $=id=>document.getElementById(id);
  let data={hosts:[],agents:[]}, host='local', instance=null, busy=false;
  const names={builtin:'Chem Agent',codex:'Codex',antigravity:'Antigravity'};
  function notify(message='', error=false, target='hosting-notice') {$(target).textContent=t(message);$(target).classList.toggle('error',error);}
  const option=(label,value)=>{const node=new Option(label,value);node.dataset.noI18n='';return node;};
  function card(title, subtitle, rows, action) {
    const details=document.createElement('details');details.className='agent-row';
    const summary=document.createElement('summary'), identity=document.createElement('span'), strong=document.createElement('strong'), sub=document.createElement('span');
    identity.className='agent-identity';strong.textContent=title;strong.dataset.noI18n='';sub.className='agent-model';sub.textContent=subtitle;identity.append(strong,sub);
    const arrow=document.createElementNS('http://www.w3.org/2000/svg','svg');arrow.classList.add('icon','disclosure');arrow.innerHTML='<use href="/assets/ui-icons.svg#i-chevron"/>';
    summary.append(identity,arrow);details.append(summary);
    const body=document.createElement('div');body.className='agent-fields';const facts=document.createElement('dl');facts.className='hosting-facts';
    for(const [label,value] of rows.filter(([,value])=>value)) {const dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=t(label);dd.textContent=value;dd.dataset.noI18n='';facts.append(dt,dd);}
    const edit=document.createElement('button');edit.type='button';edit.className='secondary';edit.textContent=t('编辑');edit.onclick=action;body.append(facts,edit);details.append(body);return details;
  }
  function render() {
    $('agent-host').replaceChildren(...data.hosts.filter(h=>h.enabled).map(h=>option(h.name,h.id)));$('agent-host').value=host;
    $('local-agent-services').hidden=host!=='local';
    const instances=data.agents.filter(a=>a.host_id===host && !['local-builtin','local-codex','local-antigravity'].includes(a.id));
    $('host-agent-list').replaceChildren(...instances.map(a=>card(a.name,`${a.name===names[a.backend]?'':names[a.backend]+' · '}${a.model || t('跟随默认模型')}${a.enabled?'':' · '+t('未启用')}`,
      [['程序路径',a.executable],['默认模型',a.model],['推理强度',a.effort]],()=>editInstance(a))));
    if(host!=='local' && !instances.length) {const empty=document.createElement('p');empty.className='hosting-empty';empty.textContent=t('此主机尚未添加 Agent');$('host-agent-list').append(empty);}
    translateUI();
  }
  async function load() {data=await api('/api/hosting');if(!data.hosts.some(h=>h.id===host&&h.enabled))host='local';render();}
  async function act(operation,target='hosting-notice') {
    if(busy)return;busy=true;notify(t('正在处理…'),false,target);
    document.querySelectorAll('.hosting-toolbar button,.hosting-form button').forEach(b=>b.disabled=true);
    try {await operation();notify(t('已保存'),false,target);} catch(error){notify(error.message,true,target);}
    finally {busy=false;document.querySelectorAll('.hosting-toolbar button,.hosting-form button').forEach(b=>b.disabled=false);}
  }
  function editInstance(row=null) {
    instance=row; $('instance-form').hidden=false;$('instance-title').textContent=t(row?'编辑 Agent':'添加 Agent');
    $('instance-name').value=row?.name || 'Codex';$('instance-backend').value=row?.backend || 'codex';
    $('instance-executable').value=row?.executable || 'codex';$('instance-model').value=row?.model || '';
    $('instance-effort').value=row?.effort || '';$('instance-enabled').checked=row?.enabled!==false;
    $('instance-name').focus();
  }
  async function saveInstance(probe=false) {
    if(!$('instance-form').reportValidity())return;
    const item={id:instance?.id || `${host}-${crypto.randomUUID()}`,host_id:host,name:$('instance-name').value.trim(),backend:$('instance-backend').value,
      executable:$('instance-executable').value.trim(),model:$('instance-model').value.trim(),effort:$('instance-effort').value.trim(),enabled:$('instance-enabled').checked};
    instance=await api(`/api/hosting/agents/${item.id}`,{method:'PUT',body:JSON.stringify(item)});
    if(probe) {
      const result=await api(`/api/hosting/agents/${item.id}/models`);
      $('instance-models').replaceChildren(...(result.models||[]).map(m=>option(m.name||m.id,m.id)));
      $('instance-efforts').replaceChildren(...[...new Set((result.models||[]).flatMap(m=>m.efforts||[]))].map(v=>option(v,v)));
      if(!result.authenticated)throw new Error(result.message || t('请在所选主机登录 Agent'));
    } else $('instance-form').hidden=true;
    await load();document.dispatchEvent(new Event('chem:agent-settings-updated'));
  }
  $('agent-host').onchange=()=>{host=$('agent-host').value;$('instance-form').hidden=true;notify();render();};
  document.querySelector('.hosting-toolbar a').onclick=event=>{event.preventDefault();selectTab('servers');};
  $('host-discover').onclick=()=>act(async()=>{data=await api(`/api/hosting/hosts/${host}/discover`,{method:'POST'});render();});
  $('host-add-agent').onclick=()=>editInstance();
  $('instance-backend').onchange=()=>{$('instance-name').value=names[$('instance-backend').value];$('instance-executable').value=$('instance-backend').value==='codex'?'codex':'agy';};
  $('instance-form').onsubmit=e=>{e.preventDefault();void act(()=>saveInstance());};
  $('instance-probe').onclick=()=>act(()=>saveInstance(true));$('instance-cancel').onclick=()=>{$('instance-form').hidden=true;};
  document.addEventListener('chem:environments-updated',()=>{void load().catch(error=>notify(error.message,true));});
  return {load};
}
