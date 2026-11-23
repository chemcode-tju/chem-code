import {escapeHtml} from './core.mjs';
import {documentBody, resultCard, memoryScope} from './memory-view.mjs';
import {t} from './i18n.mjs';


export function createMemoryWorkspace({api, markdown, updateLocation, openSession}) {
  const panel = document.getElementById('memory-page');
  const $ = id => panel.querySelector('#' + id);
  const narrow = matchMedia('(max-width:687px)');
  let generation = 0, detailGeneration = 0, offset = 0, sessionOffset = 0;
  let selected = null, searchContext = null, noteSession = null, noteProject = null;
  const sessions = new Map();
  const status = (text, error = false) => { $('status').textContent = text; $('status').classList.toggle('error', error); };
  const base = session => session ? `/api/sessions/${encodeURIComponent(session)}/memories` : "/api/research/memories";

  let activeSearch = null, sessionPage = null, visit = 0, account = 0;
  const controls = panel.querySelector('.memory-controls');
  const resultPlaceholder = $('detail').innerHTML;

  function syncLocation(context) {
    const params = new URLSearchParams(Object.entries(context).filter(([,value]) => value));
    activeSearch = params.toString();
    updateLocation(params);
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
    $('more-results').hidden = true;
    if (!context.session && context.scope === 'session') {
      $('results').setAttribute('aria-busy', 'false');
      status('请选择来源对话', true); return;
    }
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
        button.addEventListener('click',async()=>{
          await show(item.id, context.session, item.session_title);
          if (narrow.matches && selected === item.id && !panel.hidden) {
            $('detail').scrollIntoView({block:'start'});
          }
        });
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
      const source = $('detail').querySelector('[data-memory-source]');
      if (source) {
        source.addEventListener('click', event => {
          if (event.button || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
          event.preventDefault();
          void openSession(item.session_id);
        });
      }
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
    if (sessionPage) return sessionPage;
    const owner = account;
    $('more-sessions').disabled=true;
    sessionPage = api(`/sessions?limit=100&offset=${sessionOffset}`);
    try {
      const rows=await sessionPage;
      if (owner !== account) return;
      sessionOffset += rows.length; addSessions(rows); $('more-sessions').hidden=rows.length<100;
    } finally {
      if (owner === account) {
        sessionPage = null;
        $('more-sessions').disabled=false;
      }
    }
  }

  $('search-form').addEventListener('submit',search);
  for (const id of ['session','scope','kind']) $(id).addEventListener('change',search);
  $('project').addEventListener('change',()=>{ $('session').value=''; $('scope').value='project'; void search(); });
  $('query').addEventListener('search',search);
  $('more-results').addEventListener('click',event=>search(event,true));
  $('more-sessions').addEventListener('click',()=>loadSessions().catch(error=>status(error.message,true)));
  for (const button of panel.querySelectorAll('[data-close]')) button.addEventListener('click',()=>$(button.dataset.close).close());
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

  async function activate(locationSearch) {
    const params = new URLSearchParams(locationSearch);
    params.delete('embed');
    if (activeSearch === params.toString()) return;
    const turn = ++visit;
    ++generation; ++detailGeneration;
    activeSearch = null;
    $('results').replaceChildren();
    $('detail').innerHTML = resultPlaceholder;
    $('more-results').hidden = true;
    $('new-note').disabled = true;
    controls.inert = true;
    status('正在检索…');

    try {
      const projects = await api('/api/projects');
      if (turn !== visit || panel.hidden) return;
      $('project').length = 1;
      for (const project of projects) {
        const option = document.createElement('option');
        option.value = project.id; option.textContent = project.name; option.dataset.noI18n = '';
        $('project').append(option);
      }
      if (!sessionOffset) await loadSessions();
      if (turn !== visit || panel.hidden) return;
      const requested = params.get('session');
      if (requested && !sessions.has(requested)) {
        // A shared link can refer to an older chat outside the loaded list.
        try {
          const session = await api(`/sessions/${encodeURIComponent(requested)}`);
          if (turn !== visit || panel.hidden) return;
          addSessions([session]);
        } catch (error) {
          if (turn !== visit || panel.hidden) return;
          if (error.status !== 404) throw error;
        }
      }
      const session = sessions.get(requested);
      const scope = params.get('scope');
      $('session').value = session?.id || '';
      $('scope').value = ['session','project','personal'].includes(scope) ? scope :
        params.has('project_id') ? 'project' : session ? 'session' : 'personal';
      $('project').value = projects.some(project=>project.id === params.get('project_id')) ?
        params.get('project_id') : session?.project_id || '';
      $('kind').value = ['plan','calculation','lesson','document'].includes(params.get('kind')) ? params.get('kind') : '';
      $('query').value = params.get('query') || '';
      await search();
    } catch (error) {
      if (turn === visit && !panel.hidden) status(error.message, true);
    } finally {
      if (turn === visit) controls.inert = false;
    }
  }

  function closeDialogs() {
    $('note-dialog').close();
    $('versions-dialog').close();
  }

  function reset() {
    ++account; ++visit; ++generation; ++detailGeneration;
    activeSearch = null;
    sessionPage = null;
    controls.inert = false;
    sessionOffset = 0;
    sessions.clear();
    $('session').length = 1;
    $('project').length = 1;
    $('search-form').reset();
    $('save-form').reset();
    $('results').replaceChildren();
    $('detail').innerHTML = resultPlaceholder;
    closeDialogs();
  }

  return {activate, closeDialogs, reset};
}
