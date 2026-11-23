import {t, translateUI} from './i18n.mjs';

export function createHostingSettings(api) {
  const $=id=>document.getElementById(id);
  let data={hosts:[],agents:[]}, host=new URLSearchParams(location.search).get('host') || 'local', instance=null, busy=false, editorTrigger;
  const controls=()=>[$('host-add-agent'),$('host-discover'),$('agent-host'),...document.querySelectorAll('#instance-form button,#host-agent-list button')];
  const names={builtin:'Chem Agent',codex:'Codex',antigravity:'Antigravity'};
  function notify(message='', error=false, target='hosting-notice') {$(target).textContent=t(message);$(target).classList.toggle('error',error);}
  const option=(label,value)=>{const node=new Option(label,value);node.dataset.noI18n='';return node;};
  function card(agent, subtitle, rows, action) {
    const details=document.createElement('details');details.className='agent-row';
    const summary=document.createElement('summary'), identity=document.createElement('span'), strong=document.createElement('strong'), sub=document.createElement('span');
    identity.className='agent-identity';strong.textContent=agent.name;strong.dataset.noI18n='';sub.className='agent-model';sub.textContent=subtitle;identity.append(strong,sub);
    const icon = agent.backend==='antigravity'
      ? '<img src="/assets/antigravity.svg" class="agent-logo" alt="">'
      : '<svg class="icon agent-logo" aria-hidden="true"><use href="/assets/ui-icons.svg#i-gpt"/></svg>';
    summary.insertAdjacentHTML('beforeend',icon);
    summary.append(identity);
    summary.insertAdjacentHTML('beforeend','<svg class="icon disclosure" aria-hidden="true"><use href="/assets/ui-icons.svg#i-chevron"/></svg>');
    details.append(summary);
    const body=document.createElement('div');body.className='agent-fields';const facts=document.createElement('dl');facts.className='hosting-facts';
    for(const [label,value] of rows.filter(([,value])=>value)) {const dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=t(label);dd.textContent=value;dd.dataset.noI18n='';facts.append(dt,dd);}
    const edit=document.createElement('button');edit.type='button';edit.className='secondary';edit.textContent=t('编辑');edit.onclick=()=>action(edit);body.append(facts,edit);details.append(body);return details;
  }
  function render() {
    $('agent-host').replaceChildren(...data.hosts.filter(h=>h.enabled).map(h=>h.id==='local'?new Option(t('此计算机'),h.id):option(h.name,h.id)));$('agent-host').value=host;
    $('local-agent-services').hidden=host!=='local';
    const instances=data.agents.filter(a=>a.host_id===host && !['local-builtin','local-codex','local-antigravity'].includes(a.id));
    $('host-agent-list').replaceChildren(...instances.map(a=>card(a,`${a.name===names[a.backend]?'':names[a.backend]+' · '}${a.model || t('跟随默认模型')}${a.enabled?'':' · '+t('未启用')}`,
      [['程序路径',a.executable],['默认模型',a.model],['推理强度',a.effort]],trigger=>editInstance(a,trigger))));
    if(host!=='local' && !instances.length) {const empty=document.createElement('p');empty.className='hosting-empty';empty.textContent=t('此主机尚未添加 Agent');$('host-agent-list').append(empty);}
    controls().forEach(control=>{control.disabled=busy;});
    translateUI();
  }
  async function load() {
    data = await api('/api/hosting');
    if (!data.hosts.some(h => h.id === host && h.enabled)) host = 'local';
    render();
    return data;
  }
  async function act(operation, success='已保存') {
    if(busy)return;busy=true;notify(t('正在处理…'));
    controls().forEach(control=>{control.disabled=true;});
    try {await operation();notify(t(success));} catch(error){notify(error.message,true);}
    finally {busy=false;controls().forEach(control=>{control.disabled=false;});}
  }
  function editInstance(row=null, trigger=$('host-add-agent')) {
    editorTrigger=trigger;
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
  $('host-discover').onclick=()=>act(async()=>{data=await api(`/api/hosting/hosts/${host}/discover`,{method:'POST'});render();},'检测完成');
  $('host-add-agent').onclick=()=>editInstance();
  $('instance-backend').onchange=()=>{$('instance-name').value=names[$('instance-backend').value];$('instance-executable').value=$('instance-backend').value==='codex'?'codex':'agy';};
  $('instance-form').onsubmit=e=>{e.preventDefault();void act(()=>saveInstance());};
  $('instance-probe').onclick=()=>act(()=>saveInstance(true));
  $('instance-cancel').onclick=()=>{$('instance-form').hidden=true;(editorTrigger?.isConnected?editorTrigger:$('host-add-agent')).focus();};
  document.addEventListener('chem:environments-updated',()=>{void load().catch(error=>notify(error.message,true));});
  return {load};
}
