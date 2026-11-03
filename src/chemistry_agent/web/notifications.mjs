import { escapeHtml as esc } from './core.mjs';

export function createNotifications({ api }) {
  const panel = document.getElementById('notification-panel');
  const list = document.getElementById('notification-list');
  const entry=document.getElementById('notification-entry');
  const unread=document.getElementById('unread-count');
  let generation=0;
  let enabled = false, pending = false;
  function displayUnread(count) {
    unread.textContent = count || "";
    const label = count ? `通知 · ${count} 条未读` : "通知";
    entry.setAttribute("aria-label", label);
    entry.title = label;
  }
  async function refresh() {
    if (!enabled || pending) return;
    pending = true;
    const token=generation;
    try {
      const data = await api('/api/automation/notifications');
      if(token!==generation) return;
      displayUnread(data.unread);
      document.getElementById('notification-title').textContent = `通知 · ${data.unread} 条未读`;
      document.getElementById('monitor-status').textContent = data.monitor.error ||
        (data.monitor.last_success ? `后台同步于 ${new Date(data.monitor.last_success).toLocaleTimeString()}` : '后台正在同步任务…');
      list.innerHTML = data.events.map(event => `<article class="task-notification ${event.read_at?'read':''}">
        <a href="/?session=${encodeURIComponent(event.session_id)}" title="${esc(event.title)}">${esc(event.title)}</a>
        <small>${esc(new Date(event.created_at).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}))}</small>
        ${event.read_at?'':`<button type="button" data-read-event="${esc(event.id)}" aria-label="标为已读" title="标为已读"><svg class="icon" aria-hidden="true"><use href="/assets/ui-icons.svg#i-check"/></svg></button>`}</article>`).join('') || '<p>暂无通知。</p>';
    } catch (error) {
      document.getElementById('monitor-status').textContent = `通知同步暂不可用：${error.message}`;
    } finally { pending = false; }
  }
  list.addEventListener('click', async (event) => {
    const button = event.target.closest('[data-read-event]');
    if (!button) return;
    button.disabled = true;
    try { await api(`/api/automation/notifications/${encodeURIComponent(button.dataset.readEvent)}/read`, {method:'POST'}); await refresh(); }
    catch (error) { document.getElementById('monitor-status').textContent = `标记失败：${error.message}`; }
    finally { button.disabled = false; }
  });
  setInterval(() => { if (!document.hidden) refresh(); }, 15000);
  return { enabled(value) { enabled = value; entry.hidden = !value; if (!value) { generation++;panel.close();list.replaceChildren();displayUnread(0); } if (value) refresh(); }, refresh };
}
