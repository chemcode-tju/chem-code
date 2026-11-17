import {escapeHtml as esc} from '../web/core.mjs';
import {renderGeneralPlan} from '../web/general-plan.mjs';
import {mountMaps, captureMaps} from '../web/plan-map.mjs';

const $ = selector => document.querySelector(selector);
const pending = new Map();
let nextId = 0, workspace = '', surface = document.body.dataset.surface, data = null;
let selectedPlan = '', generation = 0, selectedNode = '', folderData = null;
let folderGeneration = 0, hostCapabilities = {};

function request(method, params) {
  const id = ++nextId;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {pending.delete(id);reject(new Error('宿主连接超时'));}, 40000);
    pending.set(id, {resolve, reject, timer});
    window.parent.postMessage({jsonrpc:'2.0', id, method, params}, '*');
  });
}
function notify(method, params={}) {window.parent.postMessage({jsonrpc:'2.0',method,params},'*');}
async function call(name, args) {
  const result = await request('tools/call', {name, arguments:args});
  if (result.isError) throw new Error(result.content?.[0]?.text || '读取失败');
  return result.structuredContent || JSON.parse(result.content.find(row=>row.type==='text').text);
}
function error(reason) {$('#status').textContent = reason?.message || String(reason);}
function context(value) {
  if (value?.theme) document.documentElement.dataset.theme = value.theme;
}
window.addEventListener('message', event => {
  if (event.source !== window.parent) return;
  const message = event.data;
  if (message?.jsonrpc !== '2.0') return;
  const entry = pending.get(message.id);
  if (entry) {
    clearTimeout(entry.timer); pending.delete(message.id);
    message.error ? entry.reject(new Error(message.error.message)) : entry.resolve(message.result);
  }
  if (message.method === 'ui/notifications/tool-result') {
    const value=message.params?.structuredContent;
    if(value?.workspace_id) {$('#workspace').value=value.workspace_id;openWorkspace().catch(error);}
  }
  if (message.method === 'ui/notifications/host-context-changed') context(message.params);
});

async function openWorkspace() {
  const value=$('#workspace').value.trim();
  if(!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value))
    throw new Error('请输入插件工具返回的工作空间 UUID');
  const opened=await call('workspace_open',{workspace_id:value});
  workspace=opened.id;$('#workspace-label').textContent=opened.title;
  $('#connection').open=false;
  await refresh();
}

async function refresh() {
  if(!workspace)return;
  const current=++generation;
  $('#status').textContent='读取中…';
  const next=await call(surface==='monitor'?'monitor_list_jobs':'research_read_plan',{workspace_id:workspace});
  if(current!==generation)return;
  data=next;$('#status').textContent='';
  if(surface==='monitor')renderJobs(); else renderPlan();
}

function jobsTable(jobs) {
  if(!jobs.length)return '<p class="empty">尚未登记作业</p>';
  return `<div class="table-scroll"><table><thead><tr><th>作业</th><th>调度状态</th><th>退出码</th></tr></thead><tbody>${jobs.map(job=>
    `<tr><td>${esc(job.name)}<small>${esc(job.backend==='simulated'?'模拟作业':job.host+' / '+job.scheduler_id)}</small></td><td>${esc(job.scheduler_state)}${job.observation_error?`<details><summary>读取异常</summary>${esc(job.observation_error)}</details>`:''}</td><td>${esc(job.exit_code??'—')}</td></tr>`).join('')}</tbody></table></div>`;
}
function renderJobs() {
  $('#content').innerHTML=jobsTable(data.jobs);
  $('#detail').innerHTML='<p class="muted">科学收敛未评估</p>';
}
function renderPlan() {
  const previous=captureMaps($('#content'),{dispose:true});
  const plans=data.plans;
  if(!plans.some(plan=>plan.id===selectedPlan))selectedPlan=plans[0]?.id || '';
  $('#plans').hidden=!plans.length;
  $('#plans').innerHTML=plans.map(plan=>`<option value="${esc(plan.id)}" ${plan.id===selectedPlan?'selected':''}>${esc(plan.title)} · v${plan.revision}</option>`).join('');
  const plan=plans.find(plan=>plan.id===selectedPlan);
  $('#content').innerHTML=plan?renderGeneralPlan({...plan,read_only:true}):'<p class="empty">在对话中创建研究方案后，这里会显示步骤与依赖。</p>';
  $('#detail').replaceChildren();$('#content').hidden=false;
  selectedNode='';folderGeneration++;
  mountMaps($('#content'),previous);
  for(const use of $('#content').querySelectorAll('use'))use.setAttribute('href',use.getAttribute('href').replace('/assets/ui-icons.svg',''));
}

async function showFolder(path='',offset=0) {
  const ticket=++folderGeneration;
  $('#status').textContent='';
  const result=await call('research_read_folder',{
    workspace_id:workspace,plan_id:selectedPlan,node_id:selectedNode,path,offset
  });
  if(ticket!==folderGeneration)return;
  folderData=result;$('#content').hidden=true;$('#plans').hidden=true;
  $('#detail').innerHTML=`<header class="folder-heading"><button data-back-flow>返回流程</button><h2>${esc(result.node_title)}</h2></header>
    <p class="folder-path" title="${esc(result.directory)}">${esc(result.directory)}</p>
    ${result.parent!==null?'<button data-parent-folder>上一级</button>':''}
    <ul class="file-list">${result.entries.map((entry,index)=>`<li><button data-file-index="${index}">
      <svg aria-hidden="true"><use href="#i-${entry.kind==='directory'?'folder':'document'}"/></svg>
      <span>${esc(entry.name)}</span></button></li>`).join('')}</ul>
    ${!result.entries.length?'<p class="empty">此目录暂无可显示文件</p>':''}
    <div class="file-pages">${offset?`<button data-page="${Math.max(0,offset-100)}">上一页</button>`:''}
    ${result.next_offset!==null?`<button data-page="${result.next_offset}">下一页</button>`:''}</div>`;
}
$('#content').addEventListener('click',event=>{
  const trigger=event.target.closest('[data-general-step]');if(!trigger)return;
  selectedNode=trigger.dataset.generalStep;showFolder().catch(error);
});
$('#detail').addEventListener('click',async event=>{
  try {
    if(event.target.closest('[data-back-flow]')){
      folderGeneration++;$('#detail').replaceChildren();$('#content').hidden=false;
      $('#plans').hidden=false;selectedNode='';window.dispatchEvent(new Event('resize'));return;
    }
    if(event.target.closest('[data-parent-folder]')){await showFolder(folderData.parent);return;}
    const page=event.target.closest('[data-page]');
    if(page){await showFolder(folderData.relative_path,Number(page.dataset.page));return;}
    const file=event.target.closest('[data-file-index]');if(!file)return;
    const entry=folderData.entries[Number(file.dataset.fileIndex)];
    if(entry.kind==='directory'){await showFolder(entry.path);return;}
    if(!hostCapabilities.experimental?.['openai/files'] && !hostCapabilities.extensions?.['openai/files']){
      $('#status').textContent='文件路径：'+entry.absolute_path;return;
    }
    await request('openai/files/open',{path:entry.absolute_path});
  } catch(reason){error(reason);}
});
$('#connect').addEventListener('click',()=>openWorkspace().catch(error));
$('#refresh').addEventListener('click',()=>refresh().catch(error));
$('#plans').addEventListener('change',event=>{selectedPlan=event.target.value;renderPlan();});
request('ui/initialize',{protocolVersion:'2026-01-26',appInfo:{name:'Chem Research',version:'0.1.0'},appCapabilities:{}})
  .then(result=>{hostCapabilities=result.hostCapabilities || {};context(result.hostContext);notify('ui/notifications/initialized');}).catch(error);
