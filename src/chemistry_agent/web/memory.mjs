import {createMarkdownRenderer, escapeHtml} from './core.mjs';
import {documentBody, resultCard, memoryScope} from './memory-view.mjs';
import {t} from './i18n.mjs';

const $ = id => document.getElementById(id);
const markdown = globalThis.markdownit ? createMarkdownRenderer(globalThis.markdownit) : {render:text=>`<pre>${escapeHtml(text)}</pre>`};
let generation = 0, detailGeneration = 0, offset = 0, sessionOffset = 0;
let selected = null, searchContext = null, noteSession = null, noteProject = null;
const sessions = new Map();
const status = (text, error = false) => { $('status').textContent = text; $('status').classList.toggle('error', error); };
const base = session => session ? `/api/sessions/${encodeURIComponent(session)}/memories` : "/api/research/memories";

async function api(url, options = {}) {
  const response = await fetch(url, {...options, headers:{'Content-Type':'application/json'}});
  if (!response.ok) {
    const body = await response.json().catch(()=>({}));
    throw new Error(body.error?.message || `请求失败：${response.status}`);
  }
  return response.status === 204 ? null : response.json();
}

function syncLocation(context) {
  const url = new URL(location.href);
  for (const [key,value] of Object.entries(context)) {
    if (value) url.searchParams.set(key, value); else url.searchParams.delete(key);
  }
  history.replaceState(null, '', url);
  $('workspace-link').href = context.session ? `/?session=${encodeURIComponent(context.session)}` : '/' ;
  $('new-note').disabled = context.scope === 'session' && !context.session;
  $('project-filter').hidden = context.scope !== 'project';
  $('session-filter').hidden = context.scope !== 'session';
}

async function search(event, append = false) {
  event?.preventDefault();
  const turn = ++generation;
  if (!append) {
    detailGeneration++;
    offset = 0; selected = null;
    searchContext = {...memoryScope($('scope').value,$('session').value,$('project').value), query:$('query').value.trim(), kind:$('kind').value};
    syncLocation(searchContext);
    $('results').replaceChildren();
    $('detail').replaceChildren();
  }
  const context = {...searchContext};
  if (!context.session && context.scope === 'session') {
    status('请选择来源对话', true); return;
  }
  $('more-results').hidden = true;
  $('results').setAttribute('aria-busy', 'true');
  status('正在检索…');
  const params = new URLSearchParams({scope:context.scope, query:context.query, limit:'20', offset:String(offset)});
  if (context.kind) params.set('kind',context.kind);
  if (!context.session && context.project_id) params.set('project_id',context.project_id);
  try {
    const result = await api(`${base(context.session)}?${params}`);
    if (turn !== generation) return;
    for (const item of result.items) {
      if ($('results').querySelector(`[data-memory-id="${CSS.escape(item.id)}"]`)) continue;
      const button = document.createElement('button'); button.type = 'button'; button.className = 'result-card';
      button.dataset.memoryId = item.id; button.setAttribute('aria-pressed', String(selected === item.id));
      button.innerHTML = resultCard(item);
      button.addEventListener('click',()=>show(item.id, context.session, item.session_title));
      $('results').append(button);
    }
    offset += result.items.length;
    $('more-results').hidden = !result.truncated;
    const count = $('results').querySelectorAll('button').length;
    const scopeLabel = {project:'所选项目',personal:'我的全部记忆',session:'仅此对话'}[context.scope];
    status('');
    $('status').innerHTML = `<span>${scopeLabel}</span> · <span>${result.truncated ? '已显示' : '找到'}</span> ${count} <span>条记忆</span>${result.truncated ? '<span>，可继续加载</span>' : ''}`;
    if (!count) {
      status('没有匹配的记忆');
      $('detail').replaceChildren();
      if (context.scope !== 'personal') {
        const expand = document.createElement('button'); expand.type = 'button'; expand.textContent = '搜索全部记忆';
        expand.onclick = () => { $('scope').value = 'personal'; void search(); };
        $('results').replaceChildren(expand);
      }
    } else if (!append) {
      const first = result.items[0];
      await show(first.id, context.session, first.session_title);
    }
  } catch(error) {
    if (turn !== generation) return;
    status(error.message, true);
    if (append) $('more-results').hidden=false;
    else $('detail').innerHTML='<div class="empty-state"><p>检索失败，请重试。</p></div>';
  } finally {
    if (turn === generation) $('results').setAttribute('aria-busy','false');
  }
}

async function show(identifier, session, sourceTitle) {
  const request = ++detailGeneration;
  selected = identifier;
  for (const button of $('results').querySelectorAll('button')) button.setAttribute('aria-pressed',String(button.dataset.memoryId===identifier));
  $('detail').innerHTML='<div class="empty-state"><p>正在读取文档…</p></div>';
  const path = `${base(session)}/${encodeURIComponent(identifier)}`;
  try {
    const item = await api(path);
    if (request !== detailGeneration) return;
    $('detail').innerHTML = documentBody(item, sourceTitle || sessions.get(item.session_id)?.title, markdown);
    $('detail').scrollTop = 0;
    $('detail').querySelector('[data-download]').href=`${path}/document.md`;
    $('detail').querySelector('[data-versions]').addEventListener('click',()=>versions(path));
    $('detail').querySelector('[data-forget]').addEventListener('click',async()=>{
      if (!confirm(t('移出后不再自动召回，原始计算与历史版本仍保留。'))) return;
      try { await api(`${path}/forget`, {method:'POST', body:JSON.stringify({expected_version:item.version})}); await search(); }
      catch(error) { status(error.message,true); }
    });
  } catch(error) {
    if (request !== detailGeneration) return;
    $('detail').textContent=error.message;
  }
}

async function versions(path) {
  $('versions-content').textContent='正在读取版本…'; $('versions-dialog').showModal();
  try {
    const rows=await api(`${path}/versions`);
    $('versions-content').replaceChildren();
    for (const row of rows) {
      const block=document.createElement('details'); block.className='version-row';
      const title=document.createElement('summary'); title.textContent=`v${row.version} · ${new Date(row.created_at).toLocaleString('zh-CN')}`;
      block.append(title);
      block.addEventListener('toggle',async()=>{
        if (!block.open || block.dataset.loaded) return;
        block.dataset.loaded='true';
        const text=document.createElement('pre'); text.textContent=t('正在读取…'); block.append(text);
        try { const version=await api(`${path}/versions/${row.version}`); text.textContent=version.content; }
        catch(error) { text.textContent=error.message; }
      });
      $('versions-content').append(block);
    }
  } catch(error) { $('versions-content').textContent=error.message; }
}

function addSessions(rows) {
  for (const session of rows) {
    if (sessions.has(session.id)) continue;
    sessions.set(session.id,session);
    const option=document.createElement('option'); option.value=session.id; option.textContent=session.title; option.dataset.noI18n='';
    $('session').append(option);
  }
}
async function loadSessions() {
  $('more-sessions').disabled=true;
  try {
    const rows=await api(`/sessions?limit=100&offset=${sessionOffset}`);
    sessionOffset += rows.length; addSessions(rows); $('more-sessions').hidden=rows.length<100;
  } finally { $('more-sessions').disabled=false; }
}

$('search-form').addEventListener('submit',search);
for (const id of ['session','scope','kind']) $(id).addEventListener('change',search);
$('project').addEventListener('change',()=>{ $('session').value=''; $('scope').value='project'; void search(); });
$('query').addEventListener('search',search);
$('more-results').addEventListener('click',event=>search(event,true));
$('more-sessions').addEventListener('click',()=>loadSessions().catch(error=>status(error.message,true)));
for (const button of document.querySelectorAll('[data-close]')) button.addEventListener('click',()=>$(button.dataset.close).close());
$('new-note').addEventListener('click',()=>{
  const destinationScope=memoryScope($('scope').value,$('session').value,$('project').value);
  noteSession=destinationScope.session;
  noteProject=destinationScope.project_id;
  const destination=noteSession ? sessions.get(noteSession)?.title : noteProject ? $('project').selectedOptions[0].textContent : '个人研究区';
  $('note-destination').innerHTML=`<span>保存到：</span><span${noteSession || noteProject ? ' data-no-i18n' : ''}>${escapeHtml(destination || '个人研究区')}</span>`;
  $('save-status').textContent=''; $('note-dialog').showModal();
});
$('save-form').addEventListener('submit',async event=>{
  event.preventDefault(); $('save-note').disabled=true;
  const split=id=>$(id).value.split(/[,，]/).map(value=>value.trim()).filter(Boolean);
  try {
    await api(base(noteSession) + (!noteSession && noteProject ? `?project_id=${encodeURIComponent(noteProject)}` : ''), {method:'POST', body:JSON.stringify({title:$('note-title').value, content:$('note-content').value,
      systems:split('note-systems'), tasks:split('note-tasks'), applicability:$('note-applicability').value})});
    $('save-form').reset(); $('note-dialog').close();
    $('session').value=noteSession; $('project').value=noteProject; $('scope').value=noteSession?'session':'project'; $('kind').value='document'; $('query').value='';
    await search();
  } catch(error) { $('save-status').textContent=error.message; }
  finally { $('save-note').disabled=false; }
});

try {
  const params=new URLSearchParams(location.search);
  const projects=await api('/api/projects');
  for (const project of projects) {
    const option=document.createElement('option'); option.value=project.id; option.textContent=project.name; option.dataset.noI18n='';
    $('project').append(option);
  }
  if (projects.some(project=>project.id===params.get('project_id'))) $('project').value=params.get('project_id');
  await loadSessions();
  const requested=params.get('session');
  if (requested && !sessions.has(requested)) {
    // Direct links to older conversations must work even beyond the first page.
    try { addSessions([await api(`/sessions/${encodeURIComponent(requested)}`)]); }
    catch { status('来源对话不存在或无权读取，请选择其他对话。',true); }
  }
  if (sessions.has(requested)) { $('session').value=requested; $('scope').value='session'; }
  else if (params.has('project_id')) $('scope').value='project';
  if (['session','project','personal'].includes(params.get('scope'))) $('scope').value=params.get('scope');
  if ($('scope').value==='project' && sessions.has(requested)) $('project').value=sessions.get(requested).project_id || '';
  if (['plan','calculation','lesson','document'].includes(params.get('kind'))) $('kind').value=params.get('kind');
  $('query').value=params.get('query') || '';
  await search();
} catch(error) { status(error.message,true); }
