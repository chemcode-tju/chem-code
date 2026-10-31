import { escapeHtml as esc } from './core.mjs';

export function createNotifications({ api }) {
  const panel = document.getElementById('notification-panel');
  const list = document.getElementById('notification-list');
  let enabled = false, pending = false;
  async function refresh() {
    if (!enabled || pending) return;
    pending = true;
    try {
      const data = await api('/api/automation/notifications');
      document.getElementById('notification-title').textContent = `任务通知 · ${data.unread} 条未读`;
      document.getElementById('monitor-status').textContent = data.monitor.error ||
        (data.monitor.last_success ? `后台同步于 ${new Date(data.monitor.last_success).toLocaleTimeString()}` : '后台正在同步任务…');
      list.innerHTML = data.events.map((event) => `<article class="task-notification ${event.read_at ? 'read' : ''}">
        <strong>${esc(event.title)}</strong><small>${esc(new Date(event.created_at).toLocaleString())}</small>
        ${event.detail.reason || event.detail.decision?.reason ? `<p>${esc(event.detail.reason || event.detail.decision.reason)}</p>` : ''}
        <a href="/?session=${encodeURIComponent(event.session_id)}">打开对应会话</a>
        ${event.read_at ? '' : `<button type="button" data-read-event="${esc(event.id)}">标为已读</button>`}</article>`).join('') || '<p>暂无任务通知。</p>';
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
  return { enabled(value) { enabled = value; panel.hidden = !value; if (value) refresh(); }, refresh };
}
