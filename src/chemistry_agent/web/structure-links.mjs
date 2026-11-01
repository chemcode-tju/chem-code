import { escapeHtml as esc } from './core.mjs';

export const isStructureName = name => /^(?:POSCAR|CONTCAR|XDATCAR)(?:$|[._-])|\.(?:cif|xyz|extxyz|vasp|poscar|contcar|xdatcar|traj|gro|pdb|dump|lammpstrj|data|lmp)$/i.test(name || '');
export function structureButton(kind, id, name, label = '查看结构') {
  return `<button type="button" class="view-result" data-open-kind="${esc(kind)}" data-open-id="${esc(id)}" data-open-name="${esc(name)}">${esc(label)}</button>`;
}

// Files, never model-supplied URLs, cross the same-origin viewer boundary.
export function createStructureBridge({selectTab, toast, frame = document.getElementById('structure-viewer-frame')}) {
  let ready = false, generation = 0, pending = null, timer;
  function dispatch() {
    if (!ready || !pending || !frame.contentWindow) return;
    frame.contentWindow.postMessage({type:'chem:open-structure', file:pending}, location.origin);
    pending = null; clearTimeout(timer);
  }
  window.addEventListener('message', event => {
    if (event.origin !== location.origin || event.source !== frame.contentWindow) return;
    if (event.data?.type === 'chem:viewer-ready') {ready = true; dispatch();}
    if (event.data?.type === 'chem:viewer-error') toast(String(event.data.message || '无法读取该结构'));
  });
  return {
    async open(name, fetchBlob) {
      const version = ++generation;
      pending = null; clearTimeout(timer);
      selectTab('structure');
      try {
        const blob = await fetchBlob();
        if (version !== generation) return;
        pending = blob instanceof File ? blob : new File([blob], name, {type:blob.type});
        timer = setTimeout(() => {if (version === generation && pending) toast('查看器未就绪，请重新打开结构。');}, 15000);
        dispatch();
      } catch(error) { if (version === generation) toast(error.message); }
    },
    reset() {
      generation++; pending = null; clearTimeout(timer); ready = false;
      if (frame.hasAttribute('src')) frame.removeAttribute('src');
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
