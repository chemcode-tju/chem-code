import {createCascadeMenu} from './agent-menu.mjs';
import {t} from './i18n.mjs';

export const folderLabel = path => path ? path.replace(/\/$/,'').split('/').at(-1) || '/' : '文件夹';
export function selectHost(current, host, agents) {
  const available=agents.filter(agent=>agent.host_id===host && agent.enabled);
  const agent=available.find(agent=>agent.backend===current.backend) || available[0];
  if(!agent)return null;
  if(current.host_id===host)return current;
  return {...current,host_id:host,instance_id:agent.id,backend:agent.backend,model:agent.model||'',effort:agent.effort||'',directory:'',
    permission:agent.backend==='builtin' && current.permission==='auto_review'?'user':current.permission};
}

export function createChatLocationPicker({api,current,locations,loadLocations,choose,ensureSelection,refreshModels,openSettings}) {
  const $=id=>document.getElementById(id);
  const hostButton=$('host-picker'), folderButton=$('folder-picker'), monitorButton=$('monitor-picker');
  const hosts=createCascadeMenu(hostButton), folders=createCascadeMenu(folderButton), monitors=createCascadeMenu(monitorButton);
  let generation=0;
  const hostName=id=>id==='local'?t('此计算机'):(locations().hosts.find(h=>h.id===id)?.name || id);
  const icon=name=>`<svg class="icon" aria-hidden="true"><use href="/assets/ui-icons.svg#i-${name}"/></svg>`;
  function close() {generation++;hosts.close();folders.close();monitors.close();}
  function render() {
    const value=current() || {};
    $('host-picker-label').textContent=hostName(value.host_id||'local');
    $('host-picker-label').dataset.noI18n='';
    hostButton.title=`${t('服务器')} · ${hostName(value.host_id||'local')}`;
    hostButton.querySelector('use').setAttribute('href',`/assets/ui-icons.svg#i-${!value.host_id || value.host_id==='local'?'computer':'servers'}`);
    $('folder-picker-label').textContent=value.directory?folderLabel(value.directory):t('文件夹');
    $('folder-picker-label').dataset.noI18n='';
    folderButton.title=value.directory || t('不设置文件夹');
    const monitor=value.monitor_host && value.monitor_host!=='auto'?hostName(value.monitor_host):t('自动');
    $('monitor-picker-label').textContent=`${t('作业监控')} · ${monitor}`;
    $('monitor-picker-label').dataset.noI18n='';
    monitorButton.title=`${t('作业监控')} · ${monitor}`;
  }
  async function open(menu,title,build) {
    if(menu.visible){menu.close();return;}
    await ensureSelection();if(!current())return;
    const version=++generation;menu.open(t(title),[{message:t('正在读取…')}]);
    try {await loadLocations();if(version===generation && menu.visible)menu.column(0,t(title),build());}
    catch(error){if(version===generation && menu.visible)menu.column(0,t(title),[{message:error.message}]);}
  }
  hostButton.onclick=()=>open(hosts,'服务器',()=>[
    ...locations().hosts.filter(h=>h.enabled).map(host=>({label:host.id==='local'?t('此计算机'):host.name,noTranslate:true,selected:current().host_id===host.id,
      action:async()=>{
        const next=selectHost(current(),host.id,locations().agents);
        if(next){await choose(next);void refreshModels();}
        else hosts.column(1,host.name,[{message:t('此主机尚未添加 Agent')},{label:t('设置…'),action:()=>{close();openSettings();}}]);
      }})),
    {label:t('管理服务器…'),action:()=>{close();openSettings('servers');}},
  ]);
  monitorButton.onclick=()=>open(monitors,'作业监控',()=>[
    {label:t('自动 · 跟随 Agent 主机'),selected:!current().monitor_host || current().monitor_host==='auto',action:()=>choose({...current(),monitor_host:'auto'})},
    ...locations().hosts.filter(h=>h.enabled).map(host=>({label:host.id==='local'?t('此计算机'):host.name,noTranslate:true,selected:current().monitor_host===host.id,action:()=>choose({...current(),monitor_host:host.id})})),
  ]);
  folderButton.onclick=async()=>{
    if(folders.visible){folders.close();return;}
    await ensureSelection();if(!current())return;
    const host=current().host_id || 'local', version=++generation;
    folders.open(t('文件夹'),[]);
    folders.column(0,t('文件夹'),[{label:t('不设置文件夹'),selected:!current().directory,action:()=>choose({...current(),directory:''})}],()=>{
      const box=document.createElement('div');box.className='folder-browser';
      const label=document.createElement('label');label.textContent=t('目录路径');label.htmlFor='conversation-directory';
      const row=document.createElement('div');row.className='folder-path';
      const input=document.createElement('input');input.id='conversation-directory';input.value=current().directory || '~/';input.maxLength=2048;input.spellcheck=false;
      const browse=document.createElement('button');browse.type='button';browse.innerHTML=icon('arrow-right');browse.title=t('打开目录');browse.setAttribute('aria-label',t('打开目录'));
      const list=document.createElement('div');list.className='folder-list';list.setAttribute('aria-label',t('子文件夹'));
      const status=document.createElement('p');status.className='folder-status';status.setAttribute('role','status');
      const apply=document.createElement('button');apply.type='button';apply.className='folder-apply';apply.textContent=t('使用此文件夹');
      let request=0;
      async function read(path,select=false) {
        const id=++request;input.value=path;status.textContent=t('正在读取…');apply.disabled=browse.disabled=true;
        try {
          const result=await api(`/api/hosting/hosts/${encodeURIComponent(host)}/directories?path=${encodeURIComponent(path.trim())}`);
          if(version!==generation || id!==request || !folders.visible)return;
          input.value=result.path;
          if(select){await choose({...current(),directory:result.path});return;}
          list.replaceChildren();
          const entries=[...(result.parent!==result.path?[{name:t('上一级'),path:result.parent,parent:true}]:[]),...result.directories];
          for(const entry of entries){const button=document.createElement('button');button.type='button';button.innerHTML=icon(entry.parent?'arrow':'folder');const name=document.createElement('span');name.textContent=entry.name;name.dataset.noI18n='';button.append(name);button.title=entry.path;button.onclick=()=>read(entry.path);list.append(button);}
          status.textContent=!result.exists?t('开始对话时创建此文件夹'):result.truncated?t('目录较多，可直接填写完整路径'):'';
        } catch(error){if(version===generation && id===request)status.textContent=error.message;}
        finally {if(version===generation && id===request){apply.disabled=browse.disabled=false;folders.reposition();}}
      }
      input.oninput=()=>{request++;status.textContent='';apply.disabled=browse.disabled=false;};
      browse.onclick=()=>read(input.value);apply.onclick=()=>input.value.trim()?read(input.value,true):choose({...current(),directory:''});
      input.onkeydown=event=>{if(event.key==='Enter'){event.preventDefault();void read(input.value);}};
      row.append(input,browse);box.append(label,row,list,status,apply);void read(input.value);return box;
    });
  };
  return {render,close};
}
