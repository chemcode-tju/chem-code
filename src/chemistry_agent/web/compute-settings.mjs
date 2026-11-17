import {t} from './i18n.mjs';
import {byId as $, esc} from './compute-settings-api.mjs';
import {readConnectionCheck} from './connection-checks.mjs';

export function createComputeSettings(selectTab,api) {
  let active='',generation=0,storage;
  try {storage=window.sessionStorage;} catch {}
  const overview=async()=>{
    const run=++generation;
    const [runtime,selection,health,knowledge,hosting,monitors]=await Promise.all([
      api('/api/environments').catch(()=>null), api('/api/agents/selection').catch(()=>null),
      api('/health').catch(()=>null),api('/api/knowledge/settings').catch(()=>null),
      api('/api/hosting').catch(()=>null),api('/api/hosting/monitors').catch(()=>null),
    ]);
    if(run!==generation || active!=='automation')return;
    $('runtime-host').textContent=runtime?.platform_host || t('无法读取运行位置');
    $('runtime-monitor').textContent=t(runtime ? (runtime.monitor_running?'监控运行中':'监控未运行'):'无法读取监控状态');
    const selected=selection?.backend, instance=hosting?.agents.find(a=>a.id===selection?.instance_id);
    const hostName=id=>hosting?.hosts.find(h=>h.id===id)?.name || id;
    const agentLabel=instance ? `${hostName(instance.host_id)} · ${instance.name} · ${selection.model || t('跟随默认模型')}`
      : selected==='builtin' ? `Chem Agent · ${t(health?.model_configured?'已配置':'未配置')}` : t('无法读取 Agent 配置');
    const monitorLabel=monitors?.monitors.length ? monitors.monitors.map(m=>`${hostName(m.host)} · ${m.jobs} ${t('项作业')}${m.handoffs?' · '+t('交接中'):''}${m.errors.length?' · '+t('连接待恢复'):''}`).join(' / ')
      : t('暂无监控作业');
    const nodeLabel=runtime ? t(`已启用 ${runtime.environments.filter(row=>row.enabled).length} 个节点`) : t('无法读取服务器');
    const check=knowledge?.enabled && readConnectionCheck(storage,'openviking',JSON.stringify([knowledge.endpoint,knowledge.root_uri,knowledge.has_api_key]));
    const knowledgeLabel=!knowledge ? t('无法读取知识配置') : !knowledge.enabled ? t('未启用')
      : check ? t(check.authenticated?'上次测试通过':'上次测试失败') : t('已启用 · 尚未检测');
    $('compute-steps').innerHTML=[
      ['Agent 与模型',agentLabel,'agent','model'],
      ['作业监控',monitorLabel,null,'automation'],
      ['服务器与环境',nodeLabel,'servers','servers'],
      ['知识与技能',`OpenViking · ${knowledgeLabel}`,'knowledge','memory'],
    ].map(([name,detail,target,icon])=>`<li class="overview-item"><svg class="icon overview-icon" aria-hidden="true"><use href="/assets/ui-icons.svg#i-${icon}"/></svg>
      <div class="overview-description"><strong>${esc(t(name))}</strong><small data-no-i18n>${esc(detail)}</small></div>
      ${target?`<button type="button" class="secondary overview-action" data-generic-setting="${target}">${esc(t('设置'))}<svg class="icon" aria-hidden="true"><use href="/assets/ui-icons.svg#i-chevron"/></svg></button>`:''}</li>`).join('');
  };
  $('compute-steps').onclick=event=>{
    const button=event.target.closest('[data-generic-setting]');if(button)selectTab(button.dataset.genericSetting);
  };
  window.addEventListener('languagechange',()=>{if(active==='automation')void overview();});
  return {async activate(panel) {active=panel;generation++;if(panel==='automation')await overview();}};
}
