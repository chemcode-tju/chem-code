import { t } from './i18n.mjs';
const session = location.pathname.split('/').pop();
const params = new URLSearchParams(location.search);
const host = params.get('host'),
  directory = params.get('directory');
document.querySelector('#target').textContent = host + ' · ' + directory;
document.title = host + ' · SSH';
const status = document.querySelector('#connection'),
  retry = document.querySelector('#reconnect');
const term = new globalThis.Terminal({ cursorBlink: true, convertEol: false, scrollback: 4000 });
const fit = new globalThis.FitAddon.FitAddon();
term.loadAddon(fit);
term.open(document.querySelector('#terminal'));
let socket;
function send(data) {
  if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(data));
}
term.onData((data) => send({ type: 'input', data }));
term.onResize((dim) => send({ type: 'resize', ...dim }));
new ResizeObserver(() => fit.fit()).observe(document.querySelector('#terminal'));
function connect() {
  retry.hidden = true;
  status.textContent = t('正在连接…');
  const url = new URL('/api/hosting/sessions/' + encodeURIComponent(session) + '/terminal', location.href);
  url.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  url.search = new URLSearchParams({ host, directory });
  socket = new WebSocket(url);
  socket.binaryType = 'arraybuffer';
  let error = '';
  socket.onmessage = ({ data }) => {
    if (typeof data !== 'string') {
      term.write(new Uint8Array(data));
      return;
    }
    const event = JSON.parse(data);
    if (event.error) {
      error = event.error;
      return;
    }
    status.textContent = t('已连接');
    fit.fit();
    send({ type: 'resize', cols: term.cols, rows: term.rows });
    term.focus();
  };
  socket.onclose = () => {
    status.textContent = error || t('已断开');
    retry.hidden = false;
  };
}
retry.onclick = () => {
  term.reset();
  connect();
};
connect();
