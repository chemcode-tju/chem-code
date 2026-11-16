import {escapeHtml as esc} from './core.mjs';
import {getLanguage,t} from './i18n.mjs';

export function createNotifications({api, selectSession}) {
  const panel = document.getElementById('notification-panel');
  const list = document.getElementById('notification-list');
  const entry = document.getElementById('notification-entry');
  const unread = document.getElementById('unread-count');
  const all = document.getElementById('notification-read-all');
  const status = document.getElementById('monitor-status');
  let enabled = false, generation = 0, limit = 40, signature = '';
  function displayUnread(count) {
    unread.textContent = count || '';
    entry.title = count ? t('通知') + ' · ' + count : t('通知');
    entry.setAttribute('aria-label', entry.title);
    all.disabled = !count;
  }
  function group(events, title, total) {
    if (!events.length && !total) return "";
    const rows = events.map(item => {
      const time = new Date(item.created_at);
      return `<a class="notification-row" href="/?session=${encodeURIComponent(item.session_id)}" data-notice="${esc(item.id)}"${item.read_at?'':' data-unread'}><span class="read-dot"></span><span class="notice-name" data-no-i18n>${esc(item.title)}</span><time datetime="${esc(item.created_at)}" title="${esc(time.toLocaleString())}">${esc(time.toLocaleTimeString(getLanguage(), {hour:'2-digit',minute:'2-digit'}))}</time></a>`;
    }).join('');
    return `<section class="notice-group"><h3>${t(title)}<small>${total}</small></h3><div class="notice-rows">${rows}${total>events.length?'<button data-more-notices>'+t('查看更多')+'</button>':''}</div></section>`;
  }
  async function refresh() {
    if (!enabled) return;
    const token = ++generation;
    try {
      const data = await api('/api/automation/notifications?limit='+limit);
      if (token !== generation) return;
      displayUnread(data.unread);
      status.hidden = !data.monitor?.error;
      status.textContent = data.monitor?.error || '';
      const next = JSON.stringify([data.events.map(e=>[e.id,e.read_at]), data.totals, t('异常结束')]);
      if (next === signature) return;
      signature = next;
      const normal = data.events.filter(e=>e.kind==='completed'), other = data.events.filter(e=>e.kind!=='completed');
      list.innerHTML = group(other,'异常结束',data.totals?.exception ?? other.length) + group(normal,'正常结束',data.totals?.normal ?? normal.length) || '<p>'+t('暂无通知')+'</p>';
    } catch (error) { if(token===generation) {status.hidden=false;status.textContent=error.message;} }
  }
  all.onclick = async () => {
    all.disabled=true;
    try { await api('/api/automation/notifications/read-all',{method:'POST'}); }
    catch(error) { status.hidden=false;status.textContent=error.message;all.disabled=false;return; }
    await refresh();
  };
  list.onclick = async event => {
    if (event.target.closest('[data-more-notices]')) {limit=Math.min(10000,limit+40);await refresh();return;}
    const link = event.target.closest('[data-notice]');
    if (!link || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    if (link.hasAttribute('data-unread')) {
      try { await api('/api/automation/notifications/'+encodeURIComponent(link.dataset.notice)+'/read',{method:'POST'}); }
      catch(error) { status.hidden=false;status.textContent=error.message;return; }
    }
    panel.close();
    await selectSession(new URL(link.href).searchParams.get('session'));
    void refresh();
  };
  entry.addEventListener('click',refresh);
  window.addEventListener('languagechange',refresh);
  setInterval(()=>{if(!document.hidden) void refresh();},15000);
  return {enabled(value) {
    const changed = enabled !== value;
    enabled=value;entry.hidden=!value;
    if (!changed) return;
    if (enabled) void refresh();
    else {generation++;signature='';limit=40;panel.close();list.replaceChildren();displayUnread(0);}
  },refresh};
}
