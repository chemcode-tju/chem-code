import {t} from './i18n.mjs';
import {byId as $, computeRequest, empty, esc, facts, field, hasId, record, remotePath, replaceOptions, value} from './compute-settings-api.mjs';
import {createSSHSettings} from './compute-ssh.mjs';

const panels=new Set(['automation','servers','software','potentials','queues','environments']);
const codeName=code=>[code.name || code.software || code.label,code.version].filter(Boolean).join(' ');
const stepDefinitions=[
  ['模型连接','model',['model']],
  ['本机计算服务','automation',['aiida_core','vasp_plugin','profile','daemon']],
  ['服务器连接','servers',['server']],['计算软件','software',['software']],
  ['赝势目录','potentials',['potential_source']],['执行环境','environments',['execution_target']],
];
const localLabels={aiida_core:'AiiDA 程序',vasp_plugin:'VASP 插件',profile:'本地配置',daemon:'后台服务'};
const isProbe=code=>code.plugin==='chemistry.structure_probe';
const isVasp=code=>Object.hasOwn(code,'plugin') ? code.plugin==='vasp.vasp' : /^vasp$/i.test(code.name || code.software || '');

function settingsData(raw) {
  if(!raw?.aiida || !Array.isArray(raw.servers) || !Array.isArray(raw.missing))
    throw new Error(t('未收到完整的计算配置，请刷新。'));
  return {codes:[],potential_sources:[],execution_targets:[],protocols:[],...raw};
}

export function targetPayload(form,serverId,codes,sources) {
  const id=value(form,'id'), queue=value(form,'queue'), fallback=value(form,'fallback');
  if(!hasId(id) || !hasId(queue)) throw new Error(t('环境 ID 和队列名称只能包含字母、数字、点、横线或下划线。'));
  if(fallback && (!hasId(fallback) || fallback===queue)) throw new Error(t('备用队列应与主队列不同，并使用有效名称。'));
  const code_id=value(form,'code_id'), potential_source_id=value(form,'potential_source_id');
  if(!codes.some(c=>c.id===code_id && c.server_id===serverId && isVasp(c)) ||
     !sources.some(s=>s.id===potential_source_id && s.server_id===serverId))
    throw new Error(t('请选择当前服务器上的 VASP 软件和赝势目录。'));
  const minutes=Number(value(form,'minutes'));
  if(!Number.isFinite(minutes) || minutes<0.5 || minutes>10080) throw new Error(t('时限必须在 0.5 到 10080 分钟之间。'));
  const resources={queue,wallclock_seconds:Math.round(minutes*60)};
  const limits={nodes:[1,128],ranks_per_node:[1,256],memory_mb:[128,1048576],gpus_per_node:[0,16]};
  for(const [key,[min,max]] of Object.entries(limits)) {
    const input=value(form,key), number=Number(input);
    if(!input || !Number.isInteger(number) || number<min || number>max) throw new Error(t('请检查节点、进程、内存和 GPU 数量的范围。'));
    resources[key]=number;
  }
  resources.gpu_type=resources.gpus_per_node ? value(form,'gpu_type') || null : null;
  resources.account=value(form,'account') || null;
  if([resources.gpu_type,resources.account].some(name=>name && !hasId(name))) throw new Error(t('请输入有效的 GPU 类型或调度账户名称。'));
  return {id,server_id:serverId,code_id,potential_source_id,resources,default:field(form,'default').checked,
    quota_fallback:fallback ? {source:queue,target:fallback} : null};
}

export function createComputeSettings() {
  const envForm=$('environment-form'), queues=new Map();
  let all=null, config=null, selected='', active='', generation=0, loading=false, busy=false, editing=null;
  let attempted=false, modelReady=null;
  const server=()=>all?.servers.find(row=>row.id===selected);
  const localReady=()=>Boolean(config?.aiida?.profile_ready);
  const usableCodes=()=>config?.codes.filter(c=>c.server_id===selected && isVasp(c)) || [];
  const usableSources=()=>config?.potential_sources.filter(s=>s.server_id===selected) || [];
  const ownTargets=()=>config?.execution_targets.filter(s=>s.server_id===selected) || [];
  function notify(message='',error=false) {
    $('compute-notice').textContent=t(message);
    $('compute-notice').hidden=!message || !panels.has(active);
    $('compute-notice').classList.toggle('error',error);
  }
  function availability() {
    const locked=busy || loading;
    const ready=!locked && localReady(), connected=ready && Boolean(server());
    $('active-server').disabled=locked || !all?.servers.length;
    $('compute-refresh').disabled=locked;
    $('compute-initialize').disabled=locked || !config?.aiida?.installed;
    $('server-form').querySelector('fieldset').disabled=!ready;
    for(const id of ['existing-probe','server-test','password-open','query-queues']) $(id).disabled=!connected;
    for(const id of ['code-form','potential-form']) $(id).querySelector('fieldset').disabled=!connected;
    envForm.querySelector('fieldset').disabled=!connected || !usableCodes().length || !usableSources().length;
    for(const el of document.querySelectorAll('[data-edit-target],[data-use-queue],[data-trust],#ssh-secret-form button[type=submit],#secret-clear')) el.disabled=locked;
    $('compute-scope').setAttribute('aria-busy',String(locked));
  }
  async function run(operation) {
    if(busy || loading) return;
    busy=true;availability();notify();
    try {await operation();}
    catch(error) {notify(error.message || '请求失败，请重试。',true);}
    finally {busy=false;availability();}
  }
  async function afterSave(message,preferred=selected) {
    try {await refresh(preferred);notify(message);return true;}
    catch(error) {notify(t(message)+' '+t('列表刷新失败，请点击刷新配置。')+' '+error.message,true);return false;}
  }
  const ssh=createSSHSettings({activeServer:server,run,notify,afterSave});
  function drawOverview() {
    if(!config) {$('compute-steps').replaceChildren();$('setup-default').replaceChildren();$('setup-local').hidden=true;return;}
    const missing=new Set(config.missing), aiida=config.aiida;
    if(!modelReady) missing.add('model');
    $('compute-steps').innerHTML=stepDefinitions.map(([label,panel,keys],index)=>{
      const needed=keys.filter(key=>missing.has(key));
      let detail='';
      if(panel==='automation') detail=needed.length ? needed.map(k=>t(localLabels[k])).join(' · ') : aiida.profile || 'AiiDA';
      if(panel==='model') detail=modelReady===null ? t('尚未核实') : t(modelReady?'已配置':'模型未配置');
      if(panel==='servers') {
        const check=server()?.last_connection_test;
        detail=[server()?.label, server() && t(!check?'尚未测试连接':check.ok?'上次连接测试通过':'上次连接测试未通过')].filter(Boolean).join(' · ');
      }
      if(panel==='software') detail=usableCodes().map(codeName).join(' · ');
      if(panel==='potentials') detail=usableSources().map(s=>s.root).join(' · ');
      if(panel==='environments') detail=config.default_id || '';
      return `<li><span class="step-number">${index+1}</span><div><strong>${esc(t(label))}</strong>
        <small data-no-i18n>${esc(detail)}</small></div>${panel==='automation'?`<span>${esc(t(needed.length?'待配置':'已就绪'))}</span>`:
        `<a href="#${panel}">${esc(t(needed.length?'去配置':'查看'))} ›</a>`}</li>`;
    }).join('');
    $('setup-local').hidden=aiida.profile_ready && aiida.daemon_running;
    $('compute-initialize').hidden=!aiida.installed;
    const $note=$('setup-local').querySelector('p');
    $note.textContent=t(!aiida.installed || aiida.vasp_plugin===false?'请在服务所在电脑安装所缺的计算组件，然后刷新配置。':'在运行 Chemistry Agent 的电脑上创建或启动 AiiDA 服务。');
    let message=config.ready?'计算配置已齐全。':'完成待配置项后即可准备计算。';
    if(config.ready && !modelReady) message='计算配置已齐全，请在模型页确认对话连接。';
    if(!config.default_id && ownTargets().length>1) message='有多个执行环境，请指定一个默认环境，或在对话中选择。';
    $('setup-default').innerHTML=`<p class="readiness">${esc(t(message))}${config.default_id?' <code data-no-i18n>'+esc(config.default_id)+'</code>':''}</p>`;
  }
  function drawInventory() {
    const codes=config?.codes.filter(c=>c.server_id===selected && !isProbe(c)) || [];
    $('software-list').innerHTML=codes.map(c=>record(codeName(c),[
      ['远端可执行文件',c.executable],['远端环境脚本（可选）',c.environment_script],
    ])).join('') || empty('当前服务器尚未登记软件。');
    $('potential-list').innerHTML=usableSources().map(s=>record(s.id,[['赝势库绝对路径',s.root],['赝势族',s.family],['泛函',s.functional]])).join('') || empty('当前服务器尚未关联赝势目录。');
    replaceOptions(field(envForm,'code_id'),usableCodes().map(c=>[c.id,codeName(c)]),'选择 VASP 版本');
    replaceOptions(field(envForm,'potential_source_id'),usableSources().map(s=>[s.id,s.root]),'选择赝势目录');
    $('environment-list').innerHTML=ownTargets().map(env=>{
      const res=env.resources || {},name=env.id+(env.default?' · '+t('默认'):'');
      const software=codes.find(c=>c.id===env.code_id);
      return record(name,[['软件版本',software?codeName(software):env.code_id],
        ['赝势目录',usableSources().find(s=>s.id===env.potential_source_id)?.root],['队列名称',res.queue],
        ['节点数',res.nodes],['每节点进程数',res.ranks_per_node],['时限（分钟）',res.wallclock_seconds/60],
        ['内存（MB）',res.memory_mb],['每节点 GPU 数',res.gpus_per_node],['GPU 类型（可选）',res.gpu_type],['调度账户（可选）',res.account],
        ['备用队列（可选）',env.quota_fallback?.target]],`<button type="button" data-edit-target="${esc(env.id)}">${esc(t('编辑'))}</button>`);
    }).join('') || empty('尚未建立执行环境。');
    const protocols=config?.protocols.filter(p=>p.server_id===selected) || [];
    $('legacy-environments').hidden=!protocols.length;
    $('legacy-list').innerHTML=protocols.map(p=>record(p.id,[['说明',p.description],['队列名称',p.resources?.queue]])).join('');
    const prerequisite=(!config || !localReady())?['automation','先完成本机计算服务配置。']:!server()?['servers','先添加服务器连接。']:null;
    for(const id of ['servers','software','potentials','queues','environments']) {
      const extra=prerequisite || (id==='environments' && !usableCodes().length?['software','先登记一个可执行的 VASP 软件版本。']:
        id==='environments' && !usableSources().length?['potentials','先关联当前服务器的赝势目录。']:null);
      let note=$(id).querySelector('.needs-config');
      if(!note) {note=document.createElement('p');note.className='needs-config';$(id).querySelector('.intro').after(note);}
      note.hidden=!extra || extra[0]===id;
      if(!note.hidden) note.innerHTML=`<a href="#${extra[0]}">${esc(t(extra[1]))} →</a>`;
    }
  }
  function drawQueues() {
    const stored=queues.get(selected), rows=stored?.partitions || [];
    const names=[...new Set(rows.map(row=>typeof row==='string'?row:row.queue).filter(Boolean))];
    $('known-queues').replaceChildren(...names.map(name=>new Option(name,name)));
    if(!stored) {$('queue-list').innerHTML=empty('点击查询读取当前服务器的调度信息。');return;}
    const results=rows.map(row=>typeof row==='string'?{queue:row}:row).map(row=>record(row.queue,[
      ['状态',row.availability],['时间上限',row.time_limit],['GPU',row.gres],['CPU',row.cpus],['内存（MB）',row.memory_mb],
      ['节点状态',row.state]],`<button type="button" data-use-queue="${esc(row.queue)}">${esc(t('用于执行环境'))}</button>`)).join('');
    $('queue-list').innerHTML=results || (stored.queues_output ? '<pre class="raw-queues" data-no-i18n>'+esc(stored.queues_output)+'</pre>':empty('调度器未返回队列。'));
  }
  function resetEnvironment(env=null) {
    envForm.reset();editing=env?.id || null;
    field(envForm,'id').readOnly=Boolean(env);
    $('environment-editor-label').textContent=t(env?'编辑执行环境':'添加执行环境');
    if(env) {
      const data={...env,...env.resources,minutes:env.resources.wallclock_seconds/60,fallback:env.quota_fallback?.target};
      for(const key of ['id','code_id','potential_source_id','queue','nodes','ranks_per_node','minutes','memory_mb','account','gpus_per_node','gpu_type','fallback'])
        field(envForm,key).value=data[key] ?? '';
      field(envForm,'default').checked=Boolean(env.default);
    }
    field(envForm,'gpu_type').disabled=Number(value(envForm,'gpus_per_node'))===0;
  }
  function render() {
    ssh.render(server());drawInventory();drawOverview();drawQueues();availability();
  }
  async function refresh(preferred=selected) {
    const version=++generation;loading=true;availability();
    let fetched=null, next='';
    try {
      const global=settingsData(await computeRequest('settings'));
      fetched=global;
      const defaultEnv=[...global.execution_targets,...global.protocols].find(e=>e.id===global.default_id);
      const available=id=>global.servers.some(s=>s.id===id);
      next=available(preferred)?preferred:available(defaultEnv?.server_id)?defaultEnv.server_id:global.servers[0]?.id || '';
      const scoped=next ? settingsData(await computeRequest('settings?server_id='+encodeURIComponent(next))) : global;
      if(version!==generation) return;
      const switched=next!==selected;
      all=global;config=scoped;selected=next;
      $('active-server').replaceChildren(...(global.servers.length?global.servers.map(s=>new Option(s.label,s.id)):[new Option(t('尚无服务器'),'')]));
      $('active-server').value=next;
      if(switched) {
        for(const id of ['code-editor','potential-editor','environment-editor']) $(id).open=false;
        $('code-form').reset();$('potential-form').reset();resetEnvironment();
      }
      render();
    } catch(error) {
      if(version===generation) {
        all=fetched;config=null;selected=next;
        $('active-server').replaceChildren(...(all?.servers.length?all.servers.map(s=>new Option(s.label,s.id)):[new Option('—','')]));
        $('active-server').value=next;render();
      }
      throw error;
    } finally {if(version===generation) {loading=false;availability();}}
  }
  $('active-server').onchange=()=>{notify();refresh($('active-server').value).catch(e=>notify(e.message,true));};
  $('compute-refresh').onclick=()=>{notify();refresh().catch(e=>notify(e.message,true));};
  $('compute-initialize').onclick=()=>run(async()=>{notify('正在初始化本地计算服务…');await computeRequest('settings/initialize','POST',{});await afterSave('已更新本地计算服务状态。');});
  $('code-form').onsubmit=e=>{e.preventDefault();run(async()=>{
    const form=$('code-form'),script=value(form,'environment_script');
    if(!hasId(value(form,'name')) || !hasId(value(form,'version'))) throw new Error(t('请使用软件标识，如 VASP 或 Quantum-ESPRESSO。版本可填写 6.3.2。'));
    const result=await computeRequest('codes','POST',{server_id:selected,name:value(form,'name'),version:value(form,'version'),
      executable:remotePath(value(form,'executable')),environment_script:script?remotePath(script):''});
    form.reset();$('code-editor').open=false;await afterSave('软件版本已保存。',result?.server_id || selected);
  });};
  $('potential-form').onsubmit=e=>{e.preventDefault();run(async()=>{
    const form=$('potential-form'), root=remotePath(value(form,'root'));
    if(root.split('/').some(p=>p==='..' || p==='archive')) throw new Error(t('请输入不含上级目录或 archive 的赝势路径。'));
    await computeRequest('settings/potential-sources','POST',{server_id:selected,root});
    form.reset();$('potential-editor').open=false;await afterSave('赝势目录已关联。');
  });};
  $('query-queues').onclick=()=>run(async()=>{
    notify('正在读取远端队列…');
    const id=selected;
    const result=await computeRequest('settings/servers/'+encodeURIComponent(id)+'/queues');
    queues.set(id,result);drawQueues();notify('队列查询完成。');
  });
  $('queue-list').onclick=e=>{
    const button=e.target.closest('[data-use-queue]');if(!button) return;
    resetEnvironment();drawInventory();field(envForm,'queue').value=button.dataset.useQueue;
    $('environment-editor').open=true;location.hash='environments';
  };
  $('environment-list').onclick=e=>{
    const id=e.target.closest('[data-edit-target]')?.dataset.editTarget;
    const env=ownTargets().find(row=>row.id===id);
    if(env) {resetEnvironment(env);$('environment-editor').open=true;field(envForm,'queue').focus();}
  };
  field(envForm,'gpus_per_node').oninput=()=>{field(envForm,'gpu_type').disabled=Number(value(envForm,'gpus_per_node'))===0;};
  $('environment-clear').onclick=()=>{resetEnvironment();drawInventory();};
  envForm.onsubmit=e=>{e.preventDefault();run(async()=>{
    const payload=targetPayload(envForm,selected,usableCodes(),usableSources());
    if(all.execution_targets.some(target=>target.id===payload.id) && editing!==payload.id)
      throw new Error(t('这个环境 ID 已存在。要修改它，请从已保存环境中点击编辑。'));
    await computeRequest('settings/execution-targets','POST',payload);
    resetEnvironment();$('environment-editor').open=false;await afterSave('执行环境已保存。');
  });};
  window.addEventListener('languagechange',()=>{if(config) render();ssh.translate();});
  return {activate(panel) {
    if(active!==panel) {ssh.closeSecret();notify();}
    active=panel;$('compute-scope').hidden=!panels.has(panel);
    if(panel==='automation') {
      fetch('/health',{cache:'no-store'}).then(r=>r.ok?r.json():null).then(h=>{
        modelReady=h?.model_configured ?? null; if(config) drawOverview();
      }).catch(()=>{modelReady=null; if(config) drawOverview();});
    }
    if(panels.has(panel) && !attempted) {
      attempted=true;notify('正在读取计算配置…');
      refresh().then(()=>notify()).catch(e=>notify(e.message,true));
    }
  }};
}
