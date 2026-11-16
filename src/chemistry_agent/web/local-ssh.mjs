import {byId as $, computeRequest, esc, facts} from './compute-settings-api.mjs';
import {t} from './i18n.mjs';

export function createLocalSSH() {
  let loaded=false, busy=false, servers=[];
  const shown=new Set(), checks=new Map();
  const status=(message='',error=false)=>{
    $('local-ssh-status').textContent=message;
    $('local-ssh-status').classList.toggle('error',error);
  };
  function availability() {
    for(const control of document.querySelectorAll('#local-ssh-list input,#local-ssh-list button,#local-ssh-picker select,#local-ssh-picker button')) {
      control.disabled=busy;
      if(control.dataset.localSshTest) control.disabled=busy || !servers.find(row=>row.alias===control.dataset.localSshTest)?.enabled;
    }
    $('local-ssh-add').disabled=busy || !$('local-ssh-choice').value;
    $('local-ssh-list').setAttribute('aria-busy',String(busy));
  }
  function render(data) {
    if(data) {
      for(const row of data.servers) {
        const previous=servers.find(item=>item.alias===row.alias);
        if(previous && ['hostname','user','port','source'].some(key=>previous[key]!==row[key])) checks.delete(row.alias);
      }
      servers=data.servers;
      for(const row of servers) if(row.enabled) shown.add(row.alias);
    }
    $('local-ssh-list').innerHTML=servers.filter(row=>shown.has(row.alias)).map(row=>{
      const address=[row.user,row.hostname || row.alias].filter(Boolean).join('@')+(row.port?':'+row.port:'');
      const check=checks.get(row.alias);
      const checkLabel=check ? t(check.ok?'上次测试通过':'上次测试失败') : row.enabled ? t('尚未测试连接') : '';
      return `<article class="server-card">
        <header><svg class="icon" aria-hidden="true"><use href="/assets/ui-icons.svg#i-servers"/></svg>
          <div class="server-identity"><strong data-no-i18n>${esc(row.alias)}</strong><div class="server-subtitle"><span class="server-address" data-no-i18n title="${esc(address)}">${esc(address)}</span><span class="server-check-state${check && !check.ok?' error':''}" data-ssh-result="${esc(row.alias)}" role="status">${esc(checkLabel)}</span></div></div>
          <div class="server-card-actions"><button type="button" class="secondary" data-local-ssh-test="${esc(row.alias)}">${esc(t('测试连接'))}</button><label class="server-activation"><input type="checkbox" role="switch" class="settings-switch" data-ssh-alias="${esc(row.alias)}" aria-label="${esc(t('允许 Agent 使用')+' '+row.alias)}" ${row.enabled?'checked':''}></label></div>
        </header>
        ${check?.message?`<details class="server-source"><summary>${esc(t('连接信息'))}</summary>${facts([['配置文件',row.source]])}<pre data-no-i18n>${esc(check.message)}</pre></details>`:''}
      </article>`;
    }).join('') || `<p class="server-empty">${esc(t('尚未启用服务器'))}</p>`;
    const available=servers.filter(row=>!shown.has(row.alias));
    const current=$('local-ssh-choice').value;
    $('local-ssh-choice').replaceChildren(new Option(t(available.length?'选择服务器':'没有其他 SSH 主机'),''),...available.map(row=>new Option(row.alias,row.alias)));
    if(available.some(row=>row.alias===current)) $('local-ssh-choice').value=current;
    availability();
  }
  async function run(action) {
    if(busy) return;
    busy=true;availability();status();
    try {await action();} catch(error) {status(error.message,true);}
    finally {busy=false;availability();}
  }
  async function refresh() {
    render(await computeRequest('ssh/local-aliases'));loaded=true;
  }
  async function setEnabled(alias,enabled) {
    const hosts=servers.filter(row=>row.alias===alias?enabled:row.enabled).map(row=>row.alias);
    // Keep the last confirmed server set until PUT succeeds; failures restore the switch.
    try {
      render(await computeRequest('ssh/local-aliases','PUT',{hosts}));
      status(enabled?'':t('已停用此服务器；正在运行的计算不受影响'));
    } catch(error) {render();throw error;}
  }
  $('local-ssh-refresh').onclick=()=>run(refresh);
  $('local-ssh-choice').onchange=availability;
  $('local-ssh-add').onclick=()=>run(async()=>{
    const alias=$('local-ssh-choice').value;
    if(!servers.some(row=>row.alias===alias)) return;
    await setEnabled(alias,true);
    $('local-ssh-picker').open=false;
    requestAnimationFrame(()=>$('local-ssh-list').querySelectorAll('[data-ssh-alias]').forEach(input=>{if(input.dataset.sshAlias===alias) input.focus();}));
  });
  $('local-ssh-list').onchange=event=>{
    const input=event.target.closest('[data-ssh-alias]');if(!input) return;
    const alias=input.dataset.sshAlias;
    run(async()=>{
      try {await setEnabled(alias,input.checked);}
      finally {requestAnimationFrame(()=>$('local-ssh-list').querySelectorAll('[data-ssh-alias]').forEach(node=>{if(node.dataset.sshAlias===alias) node.focus();}));}
    });
  };
  $('local-ssh-list').onclick=event=>{
    const button=event.target.closest('[data-local-ssh-test]');if(!button) return;
    const alias=button.dataset.localSshTest;
    run(async()=>{
      const output=button.closest('article').querySelector('[data-ssh-result]');
      output.textContent=t('正在连接…');
      try {
        const result=await computeRequest('ssh/local-aliases/'+encodeURIComponent(alias)+'/test','POST',{});
        checks.set(alias,{ok:result.ok,message:((result.stdout || '')+' '+(result.stderr || '')).trim()});
      } catch(error) {checks.set(alias,{ok:false,message:error.message});}
      render();
      requestAnimationFrame(()=>$('local-ssh-list').querySelectorAll('[data-local-ssh-test]').forEach(node=>{if(node.dataset.localSshTest===alias) node.focus();}));
    });
  };
  window.addEventListener('languagechange',()=>{if(loaded) render();});
  return {activate(){if(!loaded) run(refresh);}};
}
