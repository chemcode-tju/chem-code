import {escapeHtml as esc} from './core.mjs';
import {presetDescription} from './task-agent-control.mjs';
import {t} from './i18n.mjs';

const runStates = {running:'运行中',completed:'本轮已结束',failed:'运行失败',cancelled:'已取消',interrupted:'服务重启中断'};

export function createTaskThread(section, api, onAttentionChange) {
  const body = section.querySelector('[data-task-thread-body]');
  const status = section.querySelector('[data-task-thread-state]');
  let target = null, generation = 0, approvals = [], lastHtml = '', loading = false;
  let requestedApproval = null;
  const logs = new Map();

  function update(base, preset) {
    const next = preset ? `${base}/thread` : null;
    if (next === target) return;
    target = next;
    generation++;
    loading = false;
    approvals = [];
    requestedApproval = null;
    logs.clear();
    section.hidden = !target;
    section.open = false;
    section.querySelector('summary').title = presetDescription(preset);
    section.querySelector('[data-task-thread-config]').textContent = presetDescription(preset);
    status.textContent = '';
    lastHtml = '';
    body.replaceChildren();
    if (target) void refresh();
  }

  function showApproval() {
    if (!requestedApproval) return;
    const item = body.querySelector(`[data-approval-request="${CSS.escape(requestedApproval)}"]`);
    requestedApproval = null;
    if (item) {
      item.open = true;
      item.scrollIntoView({block:'nearest'});
      item.querySelector('summary').focus({preventScroll:true});
    } else status.textContent = t('这项审批已结束，请查看当前任务状态。');
  }

  function approvalMarkup(item) {
    const args = item.details;
    const job = args?.job;
    const title = job?.script ? t('审阅提交脚本') : item.title;
    const seconds = job?.resources?.wallclock_seconds;
    const walltime = !Number.isFinite(seconds) ? ''
      : seconds % 3600 === 0 ? `${seconds / 3600} h`
      : seconds % 60 === 0 ? `${seconds / 60} min` : `${seconds} s`;
    const facts = job && [job.host,job.resources?.queue,
      walltime,
      Number.isFinite(job.neb_force_target) ? `Fmax ≤ ${job.neb_force_target} eV/Å` : '',
    ].filter(Boolean).join(' · ');
    return `<details class="task-thread-approval" data-approval-request="${esc(item.id)}"><summary>${esc(title)}</summary>
      ${facts ? `<p class="step-attention-meta" data-no-i18n>${esc(facts)}</p>` : ''}
      ${job?.script ? `<pre class="task-approval-script" data-no-i18n>${esc(job.script)}</pre>` : ''}
      <details><summary>${t('请求详情')}</summary><pre>${esc(JSON.stringify(args ?? item.command ?? item, null, 2))}</pre></details>
      <div class="task-actions"><button type="button" class="secondary" data-thread-approval="${esc(item.id)}" data-approve="false">${t('拒绝')}</button><button type="button" class="primary" data-thread-approval="${esc(item.id)}" data-approve="true">${t('批准')}</button></div></details>`;
  }

  async function refresh() {
    if (!target || loading) return;
    const request = generation;
    loading = true;
    try {
      const data = await api(target);
      if (request !== generation) return;
      const newApproval = data.approvals.some(item => !approvals.some(old => old.id === item.id));
      approvals = data.approvals;
      section.dataset.threadId = data.id || '';
      const blocked = data.events.find(item => ['uncertain','blocked','retry_wait'].includes(item.status));
      status.textContent = t(approvals.length ? '待批准' : data.active_run_id ? '运行中'
        : blocked ? (blocked.status === 'uncertain' ? '待核对' : blocked.status === 'retry_wait' ? '等待重试' : '需处理') : '');
      const next = `${approvals.map(approvalMarkup).join('')}
        ${blocked?.error ? `<p role="status" data-no-i18n>${esc(blocked.error)}</p>` : ''}
        ${data.external_thread_id ? `<p class="task-thread-id" title="${esc(data.external_thread_id)}"><span>原生会话</span> <code>${esc(data.external_thread_id)}</code></p>` : ''}
        ${data.runs.length ? data.runs.map(run => `<details data-task-run="${esc(run.id)}" data-run-finished="${esc(run.finished_at || '')}"><summary>${esc(t(runStates[run.status] || run.status))} <time datetime="${esc(run.started_at)}">${esc(new Date(run.started_at).toLocaleString())}</time></summary>
          <div><pre data-no-i18n>${esc(run.answer || run.error_code || '')}</pre></div></details>`).join('') : '<p>等待任务启动</p>'}`;
      if (next !== lastHtml) {
        const open = new Set([...body.querySelectorAll('[data-task-run][open]')].map(item => item.dataset.taskRun));
        const reviewed = new Set([...body.querySelectorAll('[data-approval-request][open]')].map(item => item.dataset.approvalRequest));
        body.innerHTML = next;
        body.querySelectorAll('[data-approval-request]').forEach(item => {item.open = reviewed.has(item.dataset.approvalRequest);});
        body.querySelectorAll('[data-task-run]').forEach(item => {item.open = open.has(item.dataset.taskRun);});
        lastHtml = next;
      }
      if (newApproval) section.open = true;
      showApproval();
    } catch (error) {
      if (request === generation) status.textContent = error.message;
    } finally {
      if (request === generation) loading = false;
    }
  }

  body.addEventListener('toggle', async event => {
    const details = event.target;
    const id = details.dataset.taskRun;
    if (!details.open || !id) return;
    const request = generation;
    const output = details.lastElementChild;
    const key = `${id}:${details.dataset.runFinished}`;
    try {
      const run = logs.get(key) || await api(`/runs/${id}`);
      if (request !== generation || !details.isConnected) return;
      if (details.dataset.runFinished) logs.set(key, run);
      output.innerHTML = `<pre data-no-i18n>${esc(run.context?.error_message || run.answer || '')}</pre>`
        + (run.tool_calls || []).map(call => `<details><summary data-no-i18n>${esc(call.name || '工具调用')}</summary><pre data-no-i18n>${esc(JSON.stringify(call, null, 2))}</pre></details>`).join('');
    } catch (error) {
      if (request === generation && details.isConnected) output.textContent = error.message;
    }
  }, true);

  body.onclick = async event => {
    const button = event.target.closest('[data-thread-approval]');
    if (!button) return;
    const request = generation;
    const controls = [...button.closest('[data-approval-request]').querySelectorAll('button')];
    controls.forEach(control => {control.disabled = true;});
    try {
      await api(`/api/agents/sessions/${section.dataset.threadId}/approvals/${button.dataset.threadApproval}`, {
        method:'POST',body:JSON.stringify({approved:button.dataset.approve === 'true'}),
      });
      if (request === generation) {
        await refresh();
        section.querySelector('summary').focus({preventScroll:true});
      }
    } catch (error) {
      if (request === generation) {
        await refresh();
        status.textContent = error.message;
      }
    } finally {
      void onAttentionChange?.();
      controls.forEach(control => {control.disabled = false;});
    }
  };
  setInterval(() => {if (target && !document.hidden && section.getClientRects().length) void refresh();}, 3000);
  return {update, refresh, reveal(id) {
    requestedApproval = id;
    section.open = true;
    if (approvals.some(item => item.id === id)) showApproval();
    else void refresh();
  }};
}
