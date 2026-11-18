import {createCascadeMenu} from './agent-menu.mjs';
import {createFolderDialog} from './folder-dialog.mjs';
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
  const clearFolder=$('folder-clear');
  const hosts=createCascadeMenu(hostButton), monitors=createCascadeMenu(monitorButton);
  const folders=createFolderDialog(api,folderButton,directory=>choose({...current(),directory},{keepLocationOpen:true}));
  let generation=0;
  let nativePending=false;
  const useNativePicker = host => host==='local' && locations().desktop_folder_picker;
  const hostName=id=>id==='local'?t('此计算机'):(locations().hosts.find(h=>h.id===id)?.name || id);
  const monitorName=id=>id && id!=='auto'?hostName(id):t('自动');
  function close() {generation++;hosts.close();folders.close();monitors.close();}
  function render() {
    const value=current() || {};
    const host=value.host_id || 'local';
    const name=hostName(host);
    const visibleHost=$('composer-host');
    visibleHost.textContent=name;
    visibleHost.toggleAttribute('data-no-i18n',host!=='local');
    hostButton.title=`${t('选择服务器')} · ${name}`;
    hostButton.setAttribute('aria-label',hostButton.title);
    hostButton.querySelector('use').setAttribute('href',`/assets/ui-icons.svg#i-${host==='local'?'computer':'servers'}`);
    const visibleFolder=$('composer-folder');
    visibleFolder.textContent=value.directory?folderLabel(value.directory):t('选择项目');
    folderButton.dataset.empty=String(!value.directory);
    clearFolder.hidden=!value.directory || !useNativePicker(host);
    visibleFolder.toggleAttribute('data-no-i18n',Boolean(value.directory));
    folderButton.title=value.directory || t('选择工作文件夹');
    folderButton.setAttribute('aria-label',value.directory?`${t('选择文件夹')} · ${value.directory}`:t('选择项目'));
    folderButton.querySelector('use').setAttribute('href',`/assets/ui-icons.svg#i-${value.directory?'folder-open':'folder'}`);
    monitorButton.title=`${t('作业监控')} · ${monitorName(value.monitor_host)}`;
    monitorButton.setAttribute('aria-label',`${t('选择作业监控位置')} · ${monitorName(value.monitor_host)}`);
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
      branch:!locations().agents.some(agent=>agent.host_id===host.id && agent.enabled),
      action:async()=>{
        const next=selectHost(current(),host.id,locations().agents);
        if(next){await choose(next);void refreshModels();}
        else hosts.column(1,host.name,[{message:t('此主机尚未添加 Agent')},{label:t('配置 Agent…'),command:true,action:()=>{close();hostButton.focus();openSettings('agent',host.id);}}]);
      }})),
    {label:t('管理服务器…'),command:true,action:()=>{close();hostButton.focus();openSettings('servers');}},
  ]);
  const monitorRows=()=>[
    {label:t('自动 · 跟随 Agent 主机'),selected:!current().monitor_host || current().monitor_host==='auto',action:()=>choose({...current(),monitor_host:'auto'})},
    ...locations().hosts.filter(h=>h.enabled).map(host=>({label:hostName(host.id),noTranslate:true,selected:current().monitor_host===host.id,action:()=>choose({...current(),monitor_host:host.id})})),
  ];
  monitorButton.onclick=()=>open(monitors,'作业监控',monitorRows);
  function openBrowser(selection, host, message) {
    const label=locations().hosts.find(item=>item.id===host)?.name || hostName(host);
    folders.open({host,label,directory:selection.directory,message});
  }
  folderButton.onclick=async()=>{
    await ensureSelection();
    const selection=current();
    if(!selection || folderButton.disabled)return;
    const host=selection.host_id || 'local';
    if(!useNativePicker(host)){
      openBrowser(selection,host);
      return;
    }
    if(nativePending)return;
    const version=++generation;
    const active=()=>version===generation && current()===selection;
    nativePending=true;
    folderButton.setAttribute('aria-busy','true');
    let result;
    try {
      result=await api('/api/hosting/hosts/local/folder-picker',{
        method:'POST',body:JSON.stringify({path:selection.directory || ''}),
      });
    } catch(error) {
      if(active())openBrowser(selection,host,error.message);
    } finally {
      nativePending=false;
      folderButton.removeAttribute('aria-busy');
    }
    if(result?.path && active() && !folderButton.disabled){
      await choose({...selection,directory:result.path});
    }
  };
  clearFolder.onclick=async()=>{
    const selection=current();
    if(!selection?.directory || clearFolder.disabled || nativePending)return;
    if(await choose({...selection,directory:''}))folderButton.focus({preventScroll:true});
  };
  return {render,close};
}
