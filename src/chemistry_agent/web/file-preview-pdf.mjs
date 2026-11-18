import * as pdfjs from './vendor/pdfjs/pdf.min.mjs';
import {fileIcon} from './file-actions.mjs';
import {t} from './i18n.mjs';

const assets = '/assets/vendor/pdfjs/';
const chunkSize = 256 * 1024;
pdfjs.GlobalWorkerOptions.workerSrc = `${assets}pdf.worker.min.mjs`;

async function readRange(url, start, end, signal) {
  const response = await fetch(url, {signal, credentials:'same-origin', redirect:'error',
    headers:{Range:`bytes=${start}-${end - 1}`}});
  if (!response.ok) {
    const error = await response.json().catch(() => null);
    throw Error(t(error?.error?.message || '无法预览此 PDF。'));
  }
  const data = new Uint8Array(await response.arrayBuffer());
  const size = Number(response.headers.get('Content-Range')?.split('/').at(-1) ?? data.length);
  return {data, size};
}

export async function showPdf(target, url, signal, reportError) {
  if (signal.aborted) return;
  const transfer = new AbortController();
  signal.addEventListener('abort', () => transfer.abort(), {once:true});
  const {data, size} = await readRange(url, 0, chunkSize, transfer.signal);
  if (transfer.signal.aborted) return;
  let task, failed = false;
  function fail(error) {
    if (failed || signal.aborted) return;
    failed = true;
    transfer.abort();
    reportError(error);
  }
  class RangeTransport extends pdfjs.PDFDataRangeTransport {
    requestDataRange(start, end) {
      readRange(url, start, end, transfer.signal).then(result => {
        if (result.size !== size || result.data.length !== end - start) {
          throw Error(t('PDF 已变化，请重新打开。'));
        }
        if (!transfer.signal.aborted) this.onDataRange(start, result.data);
      }).catch(fail);
    }
    abort() {transfer.abort();}
  }
  task = pdfjs.getDocument({
    ...(data.length === size ? {data} : {range:new RangeTransport(size, data), rangeChunkSize:chunkSize}),
    disableStream:true, disableAutoFetch:true, isEvalSupported:false, enableXfa:false,
    cMapUrl:`${assets}cmaps/`, standardFontDataUrl:`${assets}standard_fonts/`,
    wasmUrl:`${assets}wasm/`, iccUrl:`${assets}iccs/`,
  });
  let rendering, observer;
  transfer.signal.addEventListener('abort', () => {
    rendering?.cancel();
    observer?.disconnect();
    void task.destroy().catch(() => {});
  }, {once:true});
  const pdf = await task.promise;
  if (transfer.signal.aborted) return;

  const viewer = document.createElement('div');
  viewer.className = 'file-pdf';
  const controls = document.createElement('div');
  controls.className = 'file-preview-controls';
  const scroll = document.createElement('div');
  scroll.className = 'file-pdf-page';
  const canvas = document.createElement('canvas');
  canvas.setAttribute('role', 'img');
  scroll.append(canvas);
  viewer.append(controls, scroll);

  let number = 1, zoom = 1, revision = 0, lastWidth = 0;
  function button(label, content, callback) {
    const control = document.createElement('button');
    control.type = 'button';
    control.title = t(label);
    control.setAttribute('aria-label', control.title);
    control.innerHTML = content;
    control.onclick = callback;
    controls.append(control);
    return control;
  }
  const previous = button('上一页', fileIcon('chevron-left'), () => navigate(number - 1));
  const page = document.createElement('input');
  page.type = 'number';
  page.min = 1;
  page.max = pdf.numPages;
  page.setAttribute('aria-label', t('页码'));
  page.onchange = () => navigate(page.valueAsNumber);
  controls.append(page, document.createTextNode(`/ ${pdf.numPages}`));
  const next = button('下一页', fileIcon('chevron'), () => navigate(number + 1));
  const smaller = button('缩小', '−', () => changeZoom(1 / 1.25));
  const larger = button('放大', '+', () => changeZoom(1.25));

  function navigate(value) {
    if (Number.isInteger(value)) number = Math.max(1, Math.min(pdf.numPages, value));
    scroll.scrollTo(0, 0);
    render();
  }
  function changeZoom(factor) {
    zoom = Math.max(.5, Math.min(4, zoom * factor));
    render();
  }
  function render() {
    page.value = number;
    previous.disabled = number === 1;
    next.disabled = number === pdf.numPages;
    smaller.disabled = zoom <= .5;
    larger.disabled = zoom >= 4;
    canvas.setAttribute('aria-label', `${t('页码')} ${number}`);
    const version = ++revision;
    rendering?.cancel();
    const width = scroll.clientWidth;
    lastWidth = width;
    if (!width) return;
    pdf.getPage(number).then(pdfPage => {
      if (version !== revision || transfer.signal.aborted) return;
      const base = pdfPage.getViewport({scale:1});
      const scale = Math.min(Math.max(1, width - 20) / base.width, 1.8) * zoom;
      const viewport = pdfPage.getViewport({scale});
      const resolution = Math.min(devicePixelRatio || 1, Math.sqrt(8e6 / (viewport.width * viewport.height)));
      canvas.width = Math.ceil(viewport.width * resolution);
      canvas.height = Math.ceil(viewport.height * resolution);
      canvas.style.width = `${viewport.width}px`;
      canvas.style.height = `${viewport.height}px`;
      rendering = pdfPage.render({canvas, viewport, transform:[resolution, 0, 0, resolution, 0, 0]});
      return rendering.promise;
    }).catch(error => {
      if (error.name !== 'RenderingCancelledException') fail(error);
    });
  }
  target.replaceChildren(viewer);
  observer = new ResizeObserver(() => {
    if (scroll.clientWidth !== lastWidth && scroll.clientWidth) render();
  });
  observer.observe(scroll);
  render();
}
