import {escapeHtml as esc} from './core.mjs';
import {fileIcon} from './file-actions.mjs';
import {t} from './i18n.mjs';
import {showFileText} from './file-preview-text.mjs';

export function showSlowDirectoryRead(show) {
  const timer = setTimeout(show, 500);
  return () => clearTimeout(timer);
}

const mediaKinds = {
  pdf:'pdf', avif:'img', bmp:'img', gif:'img', ico:'img', jpeg:'img', jpg:'img', png:'img', webp:'img',
  mp3:'audio', wav:'audio', m4a:'audio', flac:'audio', oga:'audio', ogg:'audio',
  mp4:'video', m4v:'video', webm:'video', mov:'video', ogv:'video',
};

export function fileMessage(target, text, role = 'status') {
  const element = document.createElement('p');
  element.className = 'file-browser-message';
  element.setAttribute('role', role);
  element.textContent = t(text);
  target.replaceChildren(element);
  return element;
}

export function attachmentPreviewButton(file, iconOnly = false) {
  const label = t('查看');
  return `<button type="button" class="${iconOnly?'file-action':'view-result'}" data-preview-attachment="${esc(file.id)}" data-preview-name="${esc(file.name)}" title="${esc(label)}" aria-label="${esc(label+'：'+file.name)}">${iconOnly?fileIcon('eye'):esc(label)}</button>`;
}

function addDownload(actions, name, url) {
  const link = document.createElement('a');
  link.className = 'file-action';
  link.href = url;
  link.download = name;
  link.title = t('下载');
  link.setAttribute('aria-label', t('下载'));
  link.innerHTML = fileIcon('download');
  actions.append(link);
}

function showMedia(target, kind, name, href, signal) {
  const element = document.createElement(kind);
  if (kind === 'img') element.alt = name;
  else {element.controls = true; element.preload = 'metadata';}
  element.onerror = () => {
    if (!signal.aborted) fileMessage(target, '无法预览此媒体。', 'alert');
  };
  signal.addEventListener('abort', () => {
    element.removeAttribute('src');
    if (kind !== 'img') element.load();
  }, {once:true});
  element.src = href;
  target.replaceChildren(element);
}

export async function renderFilePreview({api,name,target,actions,signal,previewUrl,contentUrl,mediaUrl}) {
  if (signal.aborted) return;
  const extension = name.includes('.') ? name.split('.').at(-1).toLowerCase() : '';
  const kind = Object.hasOwn(mediaKinds,extension) ? mediaKinds[extension] : null;
  if (kind) {
    addDownload(actions,name,contentUrl);
    if (kind === 'pdf') {
      const {showPdf} = await import('./file-preview-pdf.mjs');
      if (!signal.aborted) await showPdf(target,mediaUrl,signal,()=>fileMessage(target,'无法预览此 PDF。','alert'));
    } else showMedia(target,kind,name,mediaUrl,signal);
    return;
  }

  const data = await api(previewUrl,{signal});
  if (signal.aborted) return;
  if (data.downloadable) addDownload(actions,name,contentUrl);
  if (data.kind === 'text') showFileText(target,name,data,signal);
  else {
    const unit = new Intl.NumberFormat(undefined,{style:'unit',unit:'byte',notation:'compact'});
    fileMessage(target,'此文件格式没有文本预览。').append(` · ${unit.format(data.size)}`);
  }
}
