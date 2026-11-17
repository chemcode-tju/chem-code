import {t} from './i18n.mjs';

export function mcpConfiguration(origin,project,connection) {
  const query=project?'?'+new URLSearchParams({project_id:project}).toString():'';
  const path=connection.http_path+query;
  return {url:new URL(path,origin).href,config:{mcpServers:{'chem-knowledge':{
    command:connection.stdio_command,args:connection.stdio_args,
    env:{...connection.stdio_env,CHEM_KNOWLEDGE_URL:new URL(path,connection.platform_origin).href},
  }}}};
}

export function createMcpSettings(api) {
  const $=id=>document.getElementById(id);
  let connection=null,loading=false,timer;
  const notify=(message,error=false)=>{
    clearTimeout(timer);$('mcp-notice').textContent=message;$('mcp-notice').classList.toggle('error',error);
    if(message)timer=setTimeout(()=>{$('mcp-notice').textContent='';},error?8000:4000);
  };
  function render() {
    if(!connection)return;
    const data=mcpConfiguration(location.origin,$('mcp-project').value,connection);
    $('mcp-url').value=data.url;$('mcp-config').textContent=JSON.stringify(data.config,null,2);
  }
  $('mcp-access').addEventListener('toggle',async()=>{
    if(!$('mcp-access').open || loading)return;
    loading=true;
    try {
      const [metadata,projects]=await Promise.all([api('/api/knowledge/connection'),api('/api/projects')]);
      connection=metadata;const selected=$('mcp-project').value;
      $('mcp-project').replaceChildren(new Option(t('个人知识'),''),...projects.map(row=>{
        const option=new Option(row.name,row.id);option.dataset.noI18n='';return option;
      }));
      if(projects.some(row=>row.id===selected))$('mcp-project').value=selected;
      render();
    } catch(error) {notify(error.message,true);} finally {loading=false;}
  });
  $('mcp-project').onchange=render;
  document.querySelectorAll('[data-copy-mcp]').forEach(button=>button.onclick=async()=>{
    if(!connection)return;
    try {
      await navigator.clipboard.writeText(button.dataset.copyMcp==='url'?$('mcp-url').value:$('mcp-config').textContent);
      notify(t('已复制'));
    } catch {notify(t('复制失败，请手动选择内容。'),true);}
  });
}
