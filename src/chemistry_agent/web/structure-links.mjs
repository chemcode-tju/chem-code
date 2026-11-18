import { escapeHtml as esc } from './core.mjs';
import {fileIcon} from './file-actions.mjs';
import {t} from './i18n.mjs';

export const isStreamableStructure = name => /^(?:POSCAR|CONTCAR|XDATCAR|dump)(?:$|[._-])|\.(?:xyz|extxyz|vasp|poscar|contcar|xdatcar|traj|gro|dump|lammpstrj)$/i.test(name || '');
export const isStructureName = name => isStreamableStructure(name) || /\.(?:cif|pdb|data|lmp)$/i.test(name || '');
export function structureButton(kind, id, name, label = '查看结构', iconOnly = false) {
  return `<button type="button" class="${iconOnly?'file-action':'view-result'}"${iconOnly?` title="${esc(label)}" aria-label="${esc(label+'：'+name)}"`:''} data-open-kind="${esc(kind)}" data-open-id="${esc(id)}" data-open-name="${esc(name)}">${iconOnly?fileIcon('eye'):esc(label)}</button>`;
}

// Only files and file-browser-generated trajectory requests cross this bridge.
export function createStructureBridge({selectTab,toast,onFrame=()=>{},frame=document.getElementById('structure-viewer-frame')}) {
  let ready=false,generation=0,pending=null,timer,loaded=false,failed=false,queuedSelection=null,navigation=0;
  let handleError = () => {}, visible = null;
  function syncVisibility() {
    if (ready && visible !== null) frame.contentWindow?.postMessage({type:'chem:viewer-visibility', visible}, location.origin);
  }
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
  const events = new AbortController();
  window.addEventListener('message', event => {
    if(event.origin!==location.origin || event.source!==frame.contentWindow)return;
    const message=event.data;
    if(message?.type==='chem:viewer-ready'){ready=true;syncVisibility();dispatch();return;}
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
      if (failed) handleError();
    }
  }, {signal:events.signal});
  return {
    async open(name,fetchBlob,{selection=null,companions=[],sequence=false,signal,trajectory,onError=()=>{}}={}) {
      const version=++generation;
      handleError = onError;
      pending=null;queuedSelection=null;loaded=false;failed=false;clearTimeout(timer);
      selectTab('structure');
      try {
        let input;
        if (trajectory) {
          input = {trajectory, companions:companions.map(item=>new File([item.text],item.name))};
        } else {
          const blob=await fetchBlob({signal});
          if(version!==generation)return;
          if(sequence && (!Array.isArray(blob)||!blob.length||blob.some(item=>!(item instanceof File))))throw Error('路径结构文件无效');
          const file=sequence?blob[0]:blob instanceof File?blob:new File([blob],name,{type:blob.type});
          input = {file,...(sequence?{sequence,files:blob,name}:{}),
            companions:companions.map(item=>new File([item.text],item.name))};
        }
        pending={...input,requestId:version,selection:queuedSelection || selection};queuedSelection=null;
        timer=setTimeout(()=>{if(version===generation && pending){toast('查看器未就绪，请重新打开结构。');onError();}},15000);
        dispatch();
      }catch(error){if(version===generation && !signal?.aborted){failed=true;toast(error.message);onError();}}
    },
    seek,
    setVisible(value) {visible=value;syncVisibility();},
    dispose() {events.abort();this.reset();},
    reset() {
      generation++;pending=null;queuedSelection=null;loaded=false;failed=false;clearTimeout(timer);ready=false;
      if(frame.hasAttribute('src'))frame.removeAttribute('src');
    },
  };
}

export function createStructureDocuments({inspector, toast}) {
  const documents = new Map();
  let next = 0;
  return {
    open(name, fetchBlob, {key, title, trajectory}) {
      const previous = documents.get(key);
      if (previous) {
        inspector.select(previous.id);
        if (previous.failed) previous.load();
        return;
      }
      const id = `structure-file-${++next}`;
      const panel = document.createElement('section');
      panel.className = 'structure-document';
      const frame = document.createElement('iframe');
      frame.title = `${t('结构查看器')} · ${name}`;
      const bridge = createStructureBridge({frame, toast, selectTab:()=>inspector.select(id)});
      const controller = new AbortController();
      const intersection = new IntersectionObserver(([item]) => bridge.setVisible(item.isIntersecting));
      intersection.observe(panel);
      const entry = {id, failed:false, load() {
        entry.failed = false;
        void bridge.open(name, fetchBlob, {signal:controller.signal, trajectory, onError:()=>{entry.failed = true;}});
      }};
      documents.set(key, entry);
      panel.append(frame);
      inspector.registerDocument(id, {label:name, title, panel, dispose() {
        controller.abort();
        intersection.disconnect();
        bridge.dispose();
        documents.delete(key);
      }});
      frame.src = '/structures?embed=1';
      entry.load();
    },
    reset:inspector.clearDocuments,
  };
}

export async function getFileBlob(url, {signal} = {}) {
  const response = await fetch(url, {credentials:'same-origin', redirect:'error', signal});
  if (!response.ok) {
    const detail = await response.json().catch(()=>null);
    throw Error(response.status === 401 ? '登录已过期，请重新登录。' : detail?.error?.message || '文件暂时无法读取。');
  }
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
