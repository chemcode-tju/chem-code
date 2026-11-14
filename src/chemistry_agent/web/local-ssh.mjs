import {byId as $, computeRequest, esc, facts} from './compute-settings-api.mjs';
import {t} from './i18n.mjs';

export function createLocalSSH() {
  let loaded=false, busy=false;
  const status=message=>{$('local-ssh-status').textContent=message;};
  function render(data) {
    $('local-ssh-list').innerHTML=[...data.servers].sort((a,b)=>Number(b.enabled)-Number(a.enabled)).map(row=>`<article class="config-record">
      <header><label><input type="checkbox" data-ssh-alias="${esc(row.alias)}" ${row.enabled?'checked':''}> <span data-no-i18n>${esc(row.alias)}</span></label>
      <button type="button" data-local-ssh-test="${esc(row.alias)}" ${row.enabled?'':'disabled'}>${esc(t('测试连接'))}</button></header>
      <p data-no-i18n>${esc([row.user,row.hostname || row.alias].filter(Boolean).join('@'))}${row.port?':'+esc(row.port):''}</p>
      <details><summary>${esc(t('配置来源'))}</summary>${facts([['配置文件',row.source]])}</details>
      <p data-ssh-result="${esc(row.alias)}" role="status"></p></article>`).join('') || `<p>${esc(t('未发现本机 SSH 别名，请先配置 ~/.ssh/config。'))}</p>`;
  }
  async function run(action) {
    if(busy) return;
    busy=true;
    $('local-ssh-refresh').disabled=$('local-ssh-save').disabled=true;
    try {await action();} catch(error) {status(error.message);}
    finally {busy=false;$('local-ssh-refresh').disabled=$('local-ssh-save').disabled=false;}
  }
  async function refresh() {
    render(await computeRequest('ssh/local-aliases'));loaded=true;status('');
  }
  $('local-ssh-refresh').onclick=()=>run(refresh);
  $('local-ssh-save').onclick=()=>run(async()=>{
    const hosts=[...$('local-ssh-list').querySelectorAll('[data-ssh-alias]:checked')].map(el=>el.dataset.sshAlias);
    render(await computeRequest('ssh/local-aliases','PUT',{hosts}));
    status(t('已保存；新对话和当前对话立即生效。'));
  });
  $('local-ssh-list').onclick=event=>{
    const button=event.target.closest('[data-local-ssh-test]');if(!button) return;
    run(async()=>{
      const output=button.closest('article').querySelector('[data-ssh-result]');
      output.textContent=t('正在连接…');
      try {
        const result=await computeRequest('ssh/local-aliases/'+encodeURIComponent(button.dataset.localSshTest)+'/test','POST',{});
        output.textContent=(result.ok?t('连接成功'):t('连接失败'))+' · '+(result.stdout+' '+result.stderr).trim();
      } catch(error) {output.textContent=error.message;}
    });
  };
  return {activate(){if(!loaded) run(refresh);}};
}
