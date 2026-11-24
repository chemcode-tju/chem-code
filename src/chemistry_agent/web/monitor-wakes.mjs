import {escapeHtml as esc} from './core.mjs';
import {drawWorkflows} from './workflow-monitors.mjs';
import {getLanguage, t} from './i18n.mjs';

const base = '/api/job-monitor/wakes';
const $ = id => document.getElementById(id);
const statuses = {queued:'已排队',running:'运行中',completed:'已完成',failed:'失败',uncertain:'需核对回执'};

function period(minutes) {
  for (const [factor, unit] of [[1440,'天'],[60,'小时'],[1,'分钟']]) {
    if (minutes % factor === 0) return {count:minutes / factor, factor, unit};
  }
}

async function request(path = '', method = 'GET', value) {
  const response = await fetch(base + path, {method,cache:'no-store',
    headers:{'Content-Type':'application/json'},body:JSON.stringify(value)});
  const data = await response.json();
  if (!response.ok) {
    const error = Error(data.error?.message || t('请求未能完成，请稍后重试。'));
    error.code = data.error?.code;
    throw error;
  }
  return data;
}

export function initMonitorWakes(currentSession) {
  const form = $('wake-editor'), tasks = $('wake-tasks'), status = $('wake-status');
  const scope = $('wake-scope'), flows = $('wake-workflows');
  let workflowVersion = 0;
  const defaults = $('wake-default'), settings = $('wake-settings');
  let data, editing, editingRevision, originalDefault, version = 0, busy = false;

  function draw() {
    const options = '<option value="">全部对话</option>' + data.sessions.map(session =>
      `<option data-no-i18n value="${esc(session.id)}">${esc(session.title)}</option>`).join('');
    if (scope.dataset.options !== options) {
      const previous = scope.value;
      scope.innerHTML = options;
      scope.dataset.options = options;
      scope.value = data.sessions.some(item => item.id === previous) ? previous : '';
    }
    const scoped = data.schedules.filter(row => !scope.value || row.session_id === scope.value);
    const html = scoped.map(row => {
      const {count, unit} = period(row.interval_minutes);
      return `<article class="wake-task"><div><strong data-no-i18n>${esc(row.name)}</strong>
        <div><a data-no-i18n href="/?session=${encodeURIComponent(row.session_id)}">${esc(row.title)}</a> · <span>${row.created_by === 'agent' ? 'Agent 创建' : '用户创建'}</span></div>
        <p><span>${esc(`每 ${count} ${unit}`)}</span> · <span>${row.enabled ? '下次唤醒' : '已暂停'}</span>
        ${row.enabled ? `<time>${esc(new Date(row.next_at * 1000).toLocaleString(getLanguage(), {dateStyle:'short',timeStyle:'short'}))}</time>` : ''}</p>
        ${row.last_status ? `<p><span>最近一次</span> <time>${esc(new Date(row.last_at).toLocaleString(getLanguage(), {dateStyle:'short',timeStyle:'short'}))}</time> · <span>${esc(statuses[row.last_status] || row.last_status)}</span></p>` : ''}
        ${row.last_error ? `<p role="status">${esc(row.last_error)}</p>` : ''}</div>
        <div class="wake-actions" data-id="${esc(row.id)}"><button data-action="toggle">${row.enabled ? '暂停' : '恢复'}</button>
        <button data-action="edit">编辑</button><button data-action="remove">删除</button></div></article>`;
    }).join('') || '<p class="queue-message">暂无定时任务</p>';
    if (tasks.dataset.html !== html) {
      tasks.innerHTML = html;
      tasks.dataset.html = html;
    }
    $('wake-new').disabled = !data.sessions.length;
    $('wake-empty').hidden = Boolean(data.sessions.length);
    $('wake-disabled').hidden = data.scheduler_enabled;
  }

  async function loadWorkflows() {
    const generation = ++workflowVersion;
    const selected = scope.value;
    try {
      const result = await request('/workflows' + (selected ? '?session_id=' + encodeURIComponent(selected) : ''));
      if (generation === workflowVersion) drawWorkflows(flows, result);
    } catch (error) {
      if (generation === workflowVersion) {
        flows.textContent = error.message;
        delete flows.dataset.markup;
      }
    }
  }

  scope.onchange = () => {
    if (data) draw();
    flows.textContent = '正在读取方案…';
    delete flows.dataset.markup;
    void loadWorkflows();
  };
  $('wake-refresh').onclick = () => load();

  async function load(quiet = false) {
    const current = ++version;
    if (!data) status.textContent = '正在读取唤醒设置…';
    try {
      const latest = await request();
      if (current !== version) return;
      if (!data || defaults.value === originalDefault) defaults.value = latest.instruction;
      originalDefault = latest.instruction;
      data = latest;
      settings.hidden = false;
      if (!quiet) status.textContent = '';
      draw();
      await loadWorkflows();
    } catch (error) {
      if (current === version) status.textContent = error.message;
    }
  }

  function edit(row) {
    editing = row?.id || crypto.randomUUID();
    editingRevision = row?.revision ?? 0;
    $('wake-session').innerHTML = data.sessions.map(s =>
      `<option data-no-i18n value="${esc(s.id)}">${esc(s.title)}</option>`).join('');
    const session = row?.session_id || scope.value || currentSession?.();
    if (data.sessions.some(s => s.id === session)) $('wake-session').value = session;
    $('wake-name').value = row?.name || '';
    $('wake-prompt').value = row?.instruction || '';
    const {count,factor} = period(row?.interval_minutes || 60);
    $('wake-period').value = count;
    $('wake-unit').value = factor;
    $('wake-unit').onchange();
    form.hidden = false;
    $('wake-name').focus();
  }

  function closeEditor() {
    form.hidden = true;
    editing = null;
    $('wake-new').focus();
  }

  async function change(action) {
    if (busy) return;
    busy = true;
    settings.inert = true;
    try {
      await action();
      await load();
      if (!status.textContent) status.textContent = '已保存';
    } catch (error) {
      if (error.code === 'schedule_changed') await load(true);
      status.textContent = error.message;
    } finally {
      busy = false;
      settings.inert = false;
    }
  }

  function save(id, row) {
    const {session_id,name,instruction,interval_minutes,enabled} = row;
    return request('/schedules/' + id + '?revision=' + row.revision, 'PUT', {session_id,name,instruction,interval_minutes,enabled});
  }

  $('wake-new').onclick = () => edit();
  $('wake-cancel').onclick = closeEditor;
  $('wake-reset').onclick = () => { defaults.value = data.factory_instruction; };
  $('wake-unit').onchange = () => {
    $('wake-period').max = Math.floor(525600 / Number($('wake-unit').value));
  };
  $('wake-default-form').onsubmit = event => {
    event.preventDefault();
    void change(async () => {
      const result = await request('/defaults','PUT',{instruction:defaults.value.trim()});
      defaults.value = originalDefault = result.instruction;
    });
  };
  form.onsubmit = event => {
    event.preventDefault();
    void change(async () => {
      await save(editing, {
        revision:editingRevision, name:$('wake-name').value, session_id:$('wake-session').value,
        instruction:$('wake-prompt').value,
        interval_minutes:Number($('wake-period').value) * Number($('wake-unit').value),
        enabled:data.schedules.find(row => row.id === editing)?.enabled ?? true,
      });
      closeEditor();
    });
  };
  tasks.onclick = event => {
    const button = event.target.closest('[data-action]');
    if (!button) return;
    const row = data.schedules.find(item => item.id === button.parentElement.dataset.id);
    if (button.dataset.action === 'edit') return edit(row);
    if (button.dataset.action === 'remove' && !confirm(t('删除这个定时任务？'))) return;
    void change(async () => {
      if (button.dataset.action === 'toggle') await save(row.id,{...row,enabled:!row.enabled});
      else {
        await request('/schedules/' + row.id + '?revision=' + row.revision, 'DELETE');
        if (editing === row.id) closeEditor();
      }
    });
  };
  window.addEventListener('languagechange', () => { if (data) draw(); });
  setInterval(() => {
    if (!busy && !$('monitor-tasks-page').hidden && !document.hidden) void load(true);
  }, 15000);
  return {load};
}
