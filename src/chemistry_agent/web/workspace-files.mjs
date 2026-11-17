import {escapeHtml as esc, localFilePath} from './core.mjs';
import {isStructureName, structureButton, getFileBlob} from './structure-links.mjs';
import {fileIcon,updateStructureCount} from './file-actions.mjs';

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
  if (local !== null) return files.find(file => file.source_path === local);
  try {
    const url = new URL(href, origin);
    if (url.origin !== new URL(origin).origin) return undefined;
    const path = decodeURIComponent(url.pathname);
    return files.find(file => file.source_path === path || file.download_url === path ||
      (!href.startsWith('/') && !/^[a-z][\w+.-]*:/i.test(href) && file.path === decodeURIComponent(href).replace(/^\.\//, '')));
  } catch { return undefined; }
}

export function createWorkspaceFiles({api, session, viewer, attach, changed, toast}) {
  let current=null, files=[], generation=0;
  const expanded=new Set(), rendered=new WeakMap();
  const fileRow=file=>`<div class="workspace-file"><span data-no-i18n title="${esc(file.path)}">${esc(file.name)}</span><span class="workspace-file-actions">${isStructureName(file.name)?structureButton('file',file.id,file.name,'查看',true): /\.(png|jpe?g|webp)$/i.test(file.name)?`<button type="button" class="file-action" data-output-image="${esc(file.id)}" title="查看" aria-label="查看：${esc(file.name)}">${fileIcon('eye')}</button>`:''}<a class="file-action" href="${esc(file.download_url)}" download="${esc(file.name)}" title="下载" aria-label="下载：${esc(file.name)}">${fileIcon('download')}</a><button type="button" class="file-action" data-output-attach="${esc(file.id)}" title="用于对话" aria-label="用于对话：${esc(file.name)}">${fileIcon('cite')}</button></span></div>`;
  function group(items,key) {
    if(!items.length) return '';
    const open=expanded.has(key);
    return `<section class="workspace-outputs" aria-label="生成文件"><header>${fileIcon('document')}<span>生成文件</span><span class="file-count">${items.length}</span></header><div class="workspace-output-list">${items.slice(0,open?items.length:3).map(fileRow).join('')}</div>${items.length>3?`<button type="button" class="workspace-files-toggle" data-files-toggle="${esc(key)}" aria-expanded="${open}">${open?'收起':`展开全部 ${items.length} 个文件`}${fileIcon('chevron')}</button>`:''}</section>`;
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
    replace(document.getElementById('workspace-structures'),preferredStructures(shown).map(fileRow).join(''));
    updateStructureCount();
    for(let link of document.querySelectorAll('.markdown a[href], .markdown [data-local-file-path]')) {
      const path=link.dataset.localFilePath;
      const file=path !== undefined ? shown.find(file=>file.source_path===path) : linkedWorkspaceFile(link.getAttribute('href'),shown,location.origin);
      if(!file) continue;
      if(link.tagName !== 'A') {
        const anchor=document.createElement('a');
        anchor.dataset.localFilePath=path;anchor.title=file.name;
        anchor.append(...link.childNodes);link.replaceWith(anchor);link=anchor;
      }
      link.href=file.download_url;link.download=file.name;
      if(isStructureName(file.name) && !link.nextElementSibling?.matches('[data-open-kind]'))
        link.insertAdjacentHTML('afterend',structureButton('file',file.id,file.name,'查看结构'));
    }
  }
  document.addEventListener('click', async event=>{
    const toggle=event.target.closest('[data-files-toggle]');
    if(toggle) {
      const key=toggle.dataset.filesToggle;
      if(expanded.has(key))expanded.delete(key);else expanded.add(key);
      draw();
      document.querySelectorAll('[data-files-toggle]').forEach(button=>{if(button.dataset.filesToggle===key)button.focus();});
      return;
    }
    const button=event.target.closest('[data-output-attach],[data-output-image]');
    if(!button || current!==session()) return;
    const file=files.find(file=>file.id===(button.dataset.outputAttach || button.dataset.outputImage));
    if(!file) return;
    if(button.dataset.outputAttach) {attach(file);return;}
    try {
      const blob=await getFileBlob(file.download_url);
      const url=URL.createObjectURL(blob), dialog=document.createElement('dialog');
      dialog.className='output-image-dialog';
      dialog.innerHTML=`<button type="button" aria-label="关闭"><svg class="icon" aria-hidden="true"><use href="/assets/ui-icons.svg#i-close"/></svg></button><img alt="${esc(file.name)}" src="${url}">`;
      dialog.querySelector('button').onclick=()=>dialog.close();
      dialog.onclose=()=>{URL.revokeObjectURL(url);dialog.remove();};
      document.body.append(dialog);dialog.showModal();
    } catch(error) {toast(error.message);}
  });
  return {
    draw,
    reset() {generation++;current=null;files=[];expanded.clear();draw();},
    async refresh() {
      const id=session(), version=++generation;
      if(!id) return;
      try {
        const result=await api(`/api/workspaces/${id}/files`);
        if(id!==session() || version!==generation) return;
        current=id;files=result.files;draw();changed?.();
      } catch(error) {if(id===session()) toast('成果文件读取失败：'+error.message);}
    },
  };
}
