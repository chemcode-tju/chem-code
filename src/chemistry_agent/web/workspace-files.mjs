import {escapeHtml as esc, localFilePath} from './core.mjs';
import {isStructureName, structureButton} from './structure-links.mjs';
import {fileIcon,updateStructureCount} from './file-actions.mjs';
import {attachmentPreviewButton} from './file-preview.mjs';

export function preferredStructures(files) {
  const structures = new Map();
  for (const file of files.filter(file=>isStructureName(file.name))) {
    const key=file.path.replace(/\.(vasp|extxyz)$/i,'');
    if(!structures.has(key) || file.name.endsWith('.extxyz')) structures.set(key,file);
  }
  return [...structures.values()];
}

export function linkedWorkspaceFile(href, files, origin) {
  const local = localFilePath(href);
  if (local !== null) return files.find(file => file.source_path === local || file.sources?.some(source=>source.path===local));
  try {
    const url = new URL(href, origin);
    if (url.origin !== new URL(origin).origin) return undefined;
    const path = decodeURIComponent(url.pathname);
    return files.find(file => file.source_path === path || file.sources?.some(source=>source.path===path) || file.download_url === path ||
      (!href.startsWith('/') && !/^[a-z][\w+.-]*:/i.test(href) && file.path === decodeURIComponent(href).replace(/^\.\//, '')));
  } catch { return undefined; }
}

export function createWorkspaceFiles({api, session, attach, changed, toast, turns}) {
  let current=null, files=[], generation=0;
  const expanded=new Set(), rendered=new WeakMap();
  const fileRow=file=>`<div class="workspace-file"><span data-no-i18n title="${esc(file.path)}">${esc(file.name)}</span><span class="workspace-file-actions">${isStructureName(file.name)?structureButton('file',file.id,file.name,'查看',true):attachmentPreviewButton(file,true)}<a class="file-action" href="${esc(file.download_url)}" download="${esc(file.name)}" title="下载" aria-label="下载：${esc(file.name)}">${fileIcon('download')}</a><button type="button" class="file-action" data-output-attach="${esc(file.id)}" title="用于对话" aria-label="用于对话：${esc(file.name)}">${fileIcon('cite')}</button></span></div>`;
  function group(items,key) {
    if(!items.length) return '';
    const open=expanded.has(key);
    return `<section class="workspace-outputs" aria-label="相关文件"><header>${fileIcon('document')}<span>相关文件</span><span class="file-count">${items.length}</span></header><div class="workspace-output-list">${items.slice(0,open?items.length:3).map(fileRow).join('')}</div>${items.length>3?`<button type="button" class="workspace-files-toggle" data-files-toggle="${esc(key)}" aria-expanded="${open}">${open?'收起':`展开全部 ${items.length} 个文件`}${fileIcon('chevron')}</button>`:''}</section>`;
  }
  function replace(target,html) {
    if(rendered.get(target)===html)return;
    const scroller=target.querySelector('.workspace-output-list') || target;
    const top=scroller.scrollTop;
    target.innerHTML=html;
    (target.querySelector('.workspace-output-list') || target).scrollTop=top;
    rendered.set(target,html);
  }
  function draw() {
    const shown=current===session()?files:[];
    for(const target of document.querySelectorAll('[data-generated-run]')) {
      const key=target.dataset.generatedRun;
      replace(target,group(shown.filter(file=>file.run_id===key),key));
    }
    const byRun = new Map();
    for (const file of [...shown].reverse()) {
      if (!byRun.has(file.run_id)) byRun.set(file.run_id, []);
      byRun.get(file.run_id).push(file);
    }
    const runOrder = new Map(turns().map((turn,index)=>[turn.run?.id,index]));
    const groups = [...byRun].sort(([first],[second])=>(runOrder.get(second) ?? -1) - (runOrder.get(first) ?? -1)).map(([id, items]) => {
      const turn = turns().find(item => id && item.run?.id === id);
      const title = turn?.text?.split('\n')[0] || (id ? new Date(items[0].modified_ns / 1000000).toLocaleString() : items[0].path);
      return `<section class="result-group"><header>${id ? `<button type="button" data-origin-run="${esc(id)}" title="定位来源回复"><span data-no-i18n>${esc(title)}</span></button>` : `<strong data-no-i18n>${esc(title)}</strong>`}</header>${items.map(fileRow).join('')}</section>`;
    });
    replace(document.getElementById('session-results'),groups.join('') || '<p class="inspector-empty">暂无成果文件</p>');
    const latest = [...new Map(shown.map(file => [file.path, file])).values()];
    replace(document.getElementById('workspace-structures'),preferredStructures(latest).map(fileRow).join(''));
    updateStructureCount();
    const newest = [...shown].reverse();
    for(let link of document.querySelectorAll('.markdown a[href], .markdown [data-local-file-path]')) {
      const path=link.dataset.localFilePath;
      const file=path !== undefined ? newest.find(file=>file.source_path===path || file.sources?.some(source=>source.path===path)) : linkedWorkspaceFile(link.getAttribute('href'),newest,location.origin);
      if(!file) continue;
      if(link.tagName !== 'A') {
        const anchor=document.createElement('a');
        anchor.dataset.localFilePath=path;anchor.title=file.name;
        anchor.append(...link.childNodes);link.replaceWith(anchor);link=anchor;
      }
      link.href=file.download_url;link.download=file.name;
      if(!link.nextElementSibling?.matches('[data-open-kind],[data-preview-attachment]')) {
        const action=isStructureName(file.name)
          ? structureButton('file',file.id,file.name,'查看结构') : attachmentPreviewButton(file);
        link.insertAdjacentHTML('afterend',action);
      }
    }
  }
  document.addEventListener('click', event=>{
    const toggle=event.target.closest('[data-files-toggle]');
    if(toggle) {
      const key=toggle.dataset.filesToggle;
      if(expanded.has(key))expanded.delete(key);else expanded.add(key);
      draw();
      document.querySelectorAll('[data-files-toggle]').forEach(button=>{if(button.dataset.filesToggle===key)button.focus();});
      return;
    }
    const button=event.target.closest('[data-output-attach]');
    if(!button || current!==session()) return;
    const file=files.find(file=>file.id===button.dataset.outputAttach);
    if(file) attach(file);
  });
  return {
    draw,
    reset() {generation++;current=null;files=[];expanded.clear();draw();},
    async refresh() {
      const id=session(), version=++generation;
      if(!id) return;
      try {
        const result=await api(`/api/workspaces/${id}/files?versions=true`);
        if(id!==session() || version!==generation) return;
        current=id;files=result.files;draw();changed?.();
      } catch(error) {if(id===session()) toast('成果文件读取失败：'+error.message);}
    },
  };
}
