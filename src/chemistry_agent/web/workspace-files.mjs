import {escapeHtml as esc, localFilePath} from './core.mjs';
import {isStructureName, structureButton, getFileBlob} from './structure-links.mjs';

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
  const fileRow=file=>`<div class="workspace-file"><span data-no-i18n title="${esc(file.path)}">${esc(file.name)}</span><span class="workspace-file-actions">${isStructureName(file.name)?structureButton('file',file.id,file.name,'查看'): /\.(png|jpe?g|webp)$/i.test(file.name)?`<button type="button" data-output-image="${esc(file.id)}">查看</button>`:''}<a href="${esc(file.download_url)}" download="${esc(file.name)}">下载</a><button type="button" data-output-attach="${esc(file.id)}">用于对话</button></span></div>`;
  function group(items) {
    if(!items.length) return '';
    const structures=preferredStructures(items);
    const other=items.filter(file=>!structures.some(selected=>selected.id===file.id));
    return `<details class="workspace-outputs" open><summary>生成文件 <span>${items.length}</span></summary>${structures.map(fileRow).join('')}${other.length?`<details class="workspace-output-more"><summary>其他格式与文件 <span>${other.length}</span></summary>${other.map(fileRow).join('')}</details>`:''}</details>`;
  }
  function draw() {
    const shown=current===session()?files:[];
    for(const target of document.querySelectorAll('[data-generated-run]')) {
      target.innerHTML=group(shown.filter(file=>file.run_id===target.dataset.generatedRun));
    }
    document.getElementById('workspace-structures').innerHTML=preferredStructures(shown).map(fileRow).join('');
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
    const button=event.target.closest('[data-output-attach],[data-output-image]');
    if(!button || current!==session()) return;
    const file=files.find(file=>file.id===(button.dataset.outputAttach || button.dataset.outputImage));
    if(!file) return;
    if(button.dataset.outputAttach) {attach(file);return;}
    try {
      const blob=await getFileBlob(file.download_url);
      const url=URL.createObjectURL(blob), dialog=document.createElement('dialog');
      dialog.className='output-image-dialog';
      dialog.innerHTML=`<button type="button" aria-label="关闭">×</button><img alt="${esc(file.name)}" src="${url}">`;
      dialog.querySelector('button').onclick=()=>dialog.close();
      dialog.onclose=()=>{URL.revokeObjectURL(url);dialog.remove();};
      document.body.append(dialog);dialog.showModal();
    } catch(error) {toast(error.message);}
  });
  return {
    draw,
    reset() {generation++;current=null;files=[];draw();},
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
