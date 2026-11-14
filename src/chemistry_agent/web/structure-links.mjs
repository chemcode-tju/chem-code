import { escapeHtml as esc } from './core.mjs';
import {fileIcon} from './file-actions.mjs';

export const isStructureName = name => /^(?:POSCAR|CONTCAR|XDATCAR)(?:$|[._-])|\.(?:cif|xyz|extxyz|vasp|poscar|contcar|xdatcar|traj|gro|pdb|dump|lammpstrj|data|lmp)$/i.test(name || '');
export function structureButton(kind, id, name, label = '查看结构', iconOnly = false) {
  return `<button type="button" class="${iconOnly?'file-action':'view-result'}"${iconOnly?` title="${esc(label)}" aria-label="${esc(label+'：'+name)}"`:''} data-open-kind="${esc(kind)}" data-open-id="${esc(id)}" data-open-name="${esc(name)}">${iconOnly?fileIcon('eye'):esc(label)}</button>`;
}

// Files, never model-supplied URLs, cross the same-origin viewer boundary.
export function createStructureBridge({selectTab,toast,onFrame=()=>{},frame=document.getElementById('structure-viewer-frame')}) {
  let ready=false,generation=0,pending=null,timer,loaded=false,failed=false,queuedSelection=null,navigation=0;
  function dispatch() {
    if(!ready || !pending || !frame.contentWindow)return;
    frame.contentWindow.postMessage({type:'chem:open-structure',...pending},location.origin);
    pending=null;clearTimeout(timer);
  }
  function seek(selection) {
    if(failed)return false;
    if(!loaded) {
      if(pending)pending.selection=selection;
      else queuedSelection=selection;
      return true;
    }
    frame.contentWindow.postMessage({type:'chem:seek-structure',requestId:generation,navigationId:++navigation,selection},location.origin);
    return true;
  }
  window.addEventListener('message',event=>{
    if(event.origin!==location.origin || event.source!==frame.contentWindow)return;
    const message=event.data;
    if(message?.type==='chem:viewer-ready'){ready=true;dispatch();return;}
    if(message?.requestId!==generation)return;
    if(message.type==='chem:viewer-loaded') {
      loaded=true;
      if(queuedSelection){const selection=queuedSelection;queuedSelection=null;seek(selection);}
      else onFrame(message);
    }
    if(message.type==='chem:viewer-frame' && message.navigationId===navigation)onFrame(message);
    if(message.type==='chem:viewer-position' && loaded)onFrame(message);
    if(message.type==='chem:viewer-error' && (message.navigationId==null || message.navigationId===navigation)) {
      failed=!loaded;toast(String(message.message || '无法读取该结构'));
    }
  });
  return {
    async open(name,fetchBlob,{selection=null,companions=[],sequence=false}={}) {
      const version=++generation;
      pending=null;queuedSelection=null;loaded=false;failed=false;clearTimeout(timer);
      selectTab('structure');
      try {
        const blob=await fetchBlob();
        if(version!==generation)return;
        if(sequence && (!Array.isArray(blob)||!blob.length||blob.some(item=>!(item instanceof File))))throw Error('路径结构文件无效');
        const file=sequence?blob[0]:blob instanceof File?blob:new File([blob],name,{type:blob.type});
        pending={file,...(sequence?{sequence,files:blob,name}:{}),companions:companions.map(item=>new File([item.text],item.name)),requestId:version,selection:queuedSelection || selection};queuedSelection=null;
        timer=setTimeout(()=>{if(version===generation && pending)toast('查看器未就绪，请重新打开结构。');},15000);
        dispatch();
      }catch(error){if(version===generation){failed=true;toast(error.message);}}
    },
    seek,
    reset() {
      generation++;pending=null;queuedSelection=null;loaded=false;failed=false;clearTimeout(timer);ready=false;
      if(frame.hasAttribute('src'))frame.removeAttribute('src');
    },
  };
}

export async function getFileBlob(url) {
  const response = await fetch(url, {credentials:'same-origin', redirect:'error'});
  if (!response.ok) throw new Error(response.status === 401 ? '登录已过期，请重新登录。' : '结构文件暂时无法读取。');
  return response.blob();
}

export function inlineStructureName(text, language = '') {
  const lines = text.trimEnd().split(/\r?\n/);
  if (/^\d+$/.test(lines[0]?.trim()) && Number(lines[0]) > 0 && lines.length >= Number(lines[0]) + 2 &&
      /^\s*[A-Z][a-z]?\s+[-+.\d]/.test(lines[2] || '')) return 'structure.extxyz';
  if (/^data_/im.test(text) && /_atom_site_(?:fract|cartn)_x/i.test(text)) return 'structure.cif';
  if (lines.length >= 9 && /^[+-]?[\d.eE+-]+$/.test(lines[1]?.trim()) &&
      lines.slice(2,5).every(line => line.trim().split(/\s+/).length === 3 && line.trim().split(/\s+/).every(v=>Number.isFinite(Number(v)))) &&
      (/language-(?:vasp|poscar|contcar)/i.test(language) || lines.slice(6,10).some(line=>/^\s*(direct|cartesian)/i.test(line)))) return 'POSCAR';
  return '';
}
