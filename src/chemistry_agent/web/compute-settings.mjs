import {t} from './i18n.mjs';
import {byId as $, computeRequest, esc} from './compute-settings-api.mjs';
import {createLocalSSH} from './local-ssh.mjs';

export function createComputeSettings(selectTab) {
  const localSSH=createLocalSSH();
  let active='',generation=0;
  const overview=async()=>{
    const run=++generation;
    const [health,inventory]=await Promise.all([
      fetch('/health',{cache:'no-store'}).then(r=>r.ok?r.json():null).catch(()=>null),
      computeRequest('ssh/local-aliases').catch(()=>null),
    ]);
    if(run!==generation || active!=='automation') return;
    const serverSummary=inventory ? `已启用 ${inventory.servers.filter(row=>row.enabled).length} 台` : '无法读取服务器';
        $('compute-steps').innerHTML=[
        ['Agent 与模型',health?.model_configured?'已配置':'未配置','agent','model',Boolean(health?.model_configured)],
        ['服务器',serverSummary,'servers','servers',null],
        ['计算平台',health?.automation_enabled?'Slurm 作业监控':'计算服务尚未启用',null,'automation',Boolean(health?.automation_enabled)],
      ].map(([name,detail,target,icon,ready])=>`<li class="overview-item"><svg class="icon overview-icon" aria-hidden="true"><use href="/assets/ui-icons.svg#i-${icon}"/></svg>
        <div class="overview-description"><strong>${esc(t(name))}</strong><small>${esc(t(detail))}</small></div>
        ${target?`<button type="button" class="secondary overview-action" data-generic-setting="${target}">${esc(t('设置'))}<svg class="icon" aria-hidden="true"><use href="/assets/ui-icons.svg#i-chevron"/></svg></button>`:`<span class="overview-state" data-ready="${ready}">${esc(t(ready?'已启用':'未启用'))}</span>`}</li>`).join('');
  };
  $('compute-steps').onclick=event=>{
    const button=event.target.closest('[data-generic-setting]');
    if(button) selectTab(button.dataset.genericSetting);
  };
  window.addEventListener('languagechange',()=>{if(active==='automation') void overview();});
  return {async activate(panel) {
    active=panel;generation++;
    if(panel==='servers') localSSH.activate();
    if(panel==='automation') await overview();
  }};
}
