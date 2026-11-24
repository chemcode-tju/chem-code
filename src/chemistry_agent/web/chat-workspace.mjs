import { escapeHtml as esc } from './core.mjs';
import { t } from './i18n.mjs';

const $ = (id) => document.getElementById(id);
const labels = {
  pending: '等待送达',
  included: '已纳入本轮输入',
  delivered: '已送达本轮',
  withdrawn: '已撤回',
  expired: '本轮已结束，未送达',
  unconfirmed: '送达待核对',
  sending: '正在送达',
};
export function guidanceRows(receipt) {
  return (receipt.guidance || [])
    .map((note) => {
      const edit = note.status === 'pending';
      const reuse = ['expired', 'withdrawn'].includes(note.status);
      return `<div class="guidance-row"><span data-no-i18n>${esc(note.message)}</span><small>${esc(t(labels[note.status]))}</small>
      ${edit || reuse ? `<button type="button" data-edit-guidance="${esc(note.id)}">${t(edit ? '修改' : '放回输入框')}</button>` : ''}
      ${edit ? `<button type="button" data-withdraw-guidance="${esc(note.id)}">${t('撤回')}</button>` : ''}</div>`;
    })
    .join('');
}

export function createDelivery({ state, api, toast, sync, save }) {
  const mode = $('delivery-mode'),
    field = $('message-input');
  let draftTarget,
    editing,
    ticket,
    pending = false,
    activeId,
    explicit = false;
  mode.onchange = () => {
    explicit = true;
    save();
  };
  const running = () => state.outbox.find((item) => item.status === 'running');
  const capable = (item) => item?.native_steer || (state.health?.agent_backend === 'builtin' && item?.run_id);
  function render() {
    const live = running();
    if (dialog.open && queuedEdit?.session_id !== state.sessionId) dialog.close();
    if (draftTarget && draftTarget.session !== state.sessionId) {
      draftTarget = null;
      editing = null;
      ticket = null;
    }
    if (draftTarget && !field.value.trim()) {
      draftTarget = null;
      editing = null;
      ticket = null;
    }
    if (live?.id !== activeId) {
      explicit = false;
      activeId = live?.id;
    }
    if (!explicit && !draftTarget) mode.value = capable(live) ? 'steer' : 'queue';
    const item = draftTarget?.receipt || live;
    $('delivery-controls').hidden = !item;
    const canGuide = capable(item) || Boolean(draftTarget?.canGuide);
    mode.options[0].disabled = !canGuide;
    if (!canGuide) mode.value = 'queue';
    mode.disabled = pending;
  }
  const dialog = $('queue-edit-dialog');
  let queuedEdit;
  async function changeQueued(item, message) {
    const session = state.sessionId;
    if (item.session_id !== session) return;
    await api(`/sessions/${session}/chat-requests/${item.id}`, {
      method: 'PATCH',
      body: JSON.stringify({ previous: item.body.message, message }),
    });
    await sync();
  }
  document.addEventListener('click', async (event) => {
    const action = event.target.closest('[data-queue-edit],[data-queue-withdraw]');
    if (!action) return;
    const item = state.outbox.find(
      (row) => row.id === (action.dataset.queueEdit || action.dataset.queueWithdraw),
    );
    if (!item) return;
    if (action.dataset.queueEdit) {
      queuedEdit = item;
      $('queue-edit-text').value = item.body.message;
      dialog.showModal();
      return;
    }
    try {
      await changeQueued(item, null);
    } catch (error) {
      toast(error.message);
    }
  });
  $('queue-edit-cancel').onclick = () => dialog.close();
  $('queue-edit-form').onsubmit = async (event) => {
    event.preventDefault();
    const submit = event.submitter;
    if (submit) submit.disabled = true;
    try {
      await changeQueued(queuedEdit, $('queue-edit-text').value.trim());
      dialog.close();
    } catch (error) {
      toast(error.message);
    } finally {
      if (submit) submit.disabled = false;
    }
  };
  field.addEventListener('input', () => {
    const live = running();
    if (!draftTarget && live && field.value.trim()) draftTarget = { session: state.sessionId, receipt: live };
    if (ticket && field.value.trim() !== ticket.message) ticket = null;
    render();
  });
  document.addEventListener('click', async (event) => {
    const action = event.target.closest('[data-edit-guidance],[data-withdraw-guidance]');
    if (!action || pending) return;
    const key = action.dataset.editGuidance || action.dataset.withdrawGuidance;
    const receipt = (state.receipts || []).find((item) => item.guidance?.some((note) => note.id === key));
    if (!receipt) return;
    const note = receipt.guidance.find((item) => item.id === key);
    if (action.dataset.withdrawGuidance) {
      try {
        await api(`/sessions/${state.sessionId}/chat-requests/${receipt.id}/guidance/${key}`, {
          method: 'DELETE',
        });
        await sync();
      } catch (error) {
        toast(error.message);
      }
      return;
    }
    if (field.value.trim() && field.value.trim() !== note.message) {
      toast(t('请先保存或发送当前草稿。'));
      return;
    }
    field.value = note.message;
    editing = note.status === 'pending' ? key : null;
    draftTarget = editing ? { session: state.sessionId, receipt } : null;
    explicit = true;
    mode.value = editing ? 'steer' : 'queue';
    field.focus();
    save();
    render();
  });
  return {
    render,
    snapshot() {
      if (!draftTarget || !field.value.trim()) return null;
      const { session, receipt } = draftTarget;
      return {
        session,
        receipt: { id: receipt.id, run_id: receipt.run_id },
        mode: mode.value,
        canGuide: Boolean(capable(receipt) || draftTarget.canGuide),
        editing,
        ticket,
      };
    },
    restore(saved) {
      draftTarget = null;
      editing = null;
      ticket = null;
      explicit = false;
      if (saved?.session !== state.sessionId || !saved?.receipt?.run_id || !field.value.trim()) return;
      draftTarget = saved;
      editing = saved.editing;
      ticket = saved.ticket;
      explicit = true;
      mode.value = saved.mode === 'steer' ? 'steer' : 'queue';
      render();
    },
    async submit(hasAttachments) {
      const item = draftTarget?.receipt || running();
      if (!item || mode.value !== 'steer') {
        draftTarget = null;
        editing = null;
        ticket = null;
        return false;
      }
      if (pending) return true;
      if (hasAttachments) {
        toast(t('带附件或方案修改请选「下一轮再做」。'));
        return true;
      }
      const message = field.value.trim(),
        session = state.sessionId;
      if (!message) return true;
      draftTarget = { session, receipt: item, canGuide: mode.value === 'steer' };
      if (!ticket || ticket.target !== item.id || ticket.message !== message)
        ticket = { target: item.id, message, id: editing || crypto.randomUUID() };
      pending = true;
      save();
      render();
      try {
        await api(`/sessions/${session}/chat-requests/${item.id}/guidance/${ticket.id}`, {
          method: 'PUT',
          body: JSON.stringify({ run_id: item.run_id, message }),
        });
        if (state.sessionId === session && field.value.trim() === message) {
          field.value = '';
          save();
          draftTarget = null;
          editing = null;
          ticket = null;
        }
        await sync();
      } catch (error) {
        toast(error.message);
      } finally {
        pending = false;
        render();
      }
      return true;
    },
  };
}

export function createSideChat({ api, state, markdown, toast }) {
  const field = $('side-input'),
    form = $('side-form'),
    history = $('side-history');
  let owner = null,
    generation = 0,
    items = [],
    drafts = new Map(),
    pending = new Map();
  function draw() {
    const busy = pending.has(owner) || items.some((item) => !item.answer && !item.error);
    field.disabled = !owner;
    $('side-submit').disabled = !owner || busy;
    history.innerHTML =
      items
        .map(
          (item) => `<article><p class="side-question" data-no-i18n>${esc(item.question)}</p>
      ${
        item.answer
          ? `<div class="markdown" data-no-i18n>${markdown.render(item.answer)}</div>`
          : `<p role="status">${esc(item.error || t('正在回答…'))}</p>`
      }</article>`,
        )
        .join('') ||
      `<p class="inspector-empty">${t(owner ? '在这里追问当前对话' : '请先打开一个对话。')}</p>`;
  }
  async function refresh() {
    const id = owner,
      version = ++generation;
    if (!id || $('inspector-panel-side').hidden) return;
    try {
      const data = await api(`/sessions/${id}/side-chat`);
      if (version !== generation || id !== state.sessionId) return;
      const sending = pending.get(id);
      items = data.items;
      if (sending && !items.some((item) => item.id === sending.id)) items.push(sending);
      draw();
    } catch (error) {
      if (owner === id) history.textContent = error.message;
    }
  }
  function select(id) {
    if (id === owner) return;
    generation++;
    owner = id;
    items = [];
    field.value = drafts.get(id) || '';
    draw();
    void refresh();
  }
  field.oninput = () => drafts.set(owner, field.value);
  field.onkeydown = (event) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      form.requestSubmit();
    }
  };
  form.onsubmit = async (event) => {
    event.preventDefault();
    const id = owner,
      question = field.value.trim();
    if (!id || !question || $('side-submit').disabled) return;
    const item = { id: crypto.randomUUID(), question };
    pending.set(id, item);
    items.push(item);
    draw();
    field.value = '';
    drafts.delete(id);
    const version = state.accountVersion;
    try {
      const reply = await api(`/sessions/${id}/side-chat`, { method: 'POST', body: JSON.stringify(item) });
      if (reply.error && owner === id) toast(reply.error);
    } catch (error) {
      if (owner === id && version === state.accountVersion) {
        toast(error.message);
        if (!field.value) field.value = question;
      }
    } finally {
      pending.delete(id);
      if (owner === id && version === state.accountVersion) {
        await refresh();
        history.scrollTop = history.scrollHeight;
      }
    }
  };
  new MutationObserver(() => {
    if (!$('inspector-panel-side').hidden) void refresh();
  }).observe($('inspector-panel-side'), { attributes: true, attributeFilter: ['hidden'] });
  setInterval(() => {
    if (!document.hidden && items.some((item) => !item.answer && !item.error) && !pending.has(owner))
      void refresh();
  }, 2500);
  return {
    select,
    reset() {
      drafts.clear();
      pending.clear();
      select(null);
    },
  };
}

export function createTargetMenu({ api, state, openFolder, toast }) {
  const menu = $('workspace-target'),
    link = $('target-terminal');
  let selection = null,
    resolved = null,
    version = 0,
    session = null;
  const picker = $('terminal-target');
  function terminalTarget() {
    const target = resolved?.targets[Number(picker.value)];
    link.hidden = !target;
    if (target) {
      link.href =
        '/terminal/' +
        session +
        '?' +
        new URLSearchParams({ host: target.host, directory: target.directory });
      link.textContent = t('打开终端') + ' · ' + target.host;
      link.title = target.directory;
    } else link.removeAttribute('href');
  }
  picker.onchange = terminalTarget;
  function clear() {
    version++;
    menu.open = false;
    resolved = null;
    picker.hidden = true;
    link.hidden = true;
    link.removeAttribute('href');
  }
  function render(value) {
    if (
      value?.directory !== selection?.directory ||
      value?.host_id !== selection?.host_id ||
      session !== state.sessionId
    )
      clear();
    session = state.sessionId;
    selection = value;
    menu.hidden = !value || !session || Boolean(state.auth.enabled);
    $('target-host').textContent = value?.host_id === 'local' ? t('本机') : value?.host_id || '';
  }
  menu.ontoggle = async () => {
    if (!menu.open) return;
    const id = session,
      generation = ++version;
    $('target-path').textContent = t('正在读取…');
    link.hidden = true;
    picker.hidden = true;
    $('target-folder').disabled = $('target-copy').disabled = true;
    try {
      const data = await api(`/api/hosting/sessions/${id}/location`);
      if (version !== generation || id !== state.sessionId || !menu.open) return;
      resolved = data;
      $('target-path').textContent = data.directory;
      $('target-folder').disabled = $('target-copy').disabled = false;
      picker.replaceChildren(...data.targets.map((target, index) => new Option(target.label, index)));
      picker.hidden = data.targets.length < 2;
      terminalTarget();
    } catch (error) {
      if (version === generation) $('target-path').textContent = error.message;
    }
  };
  $('target-copy').onclick = () =>
    navigator.clipboard
      .writeText(resolved.directory)
      .then(() => (menu.open = false))
      .catch((error) => toast(error.message));
  $('target-folder').onclick = async () => {
    menu.open = false;
    try {
      if (resolved) await openFolder(resolved);
    } catch (error) {
      toast(error.message);
    }
  };
  menu.addEventListener('click', (e) => {
    if (e.target.closest('[data-open-inspector],a')) menu.open = false;
  });
  document.addEventListener('click', (e) => {
    if (menu.open && !menu.contains(e.target)) menu.open = false;
  });
  menu.onkeydown = (e) => {
    if (e.key === 'Escape') {
      menu.open = false;
      menu.querySelector('summary').focus();
    }
  };
  return { render, clear };
}

export function createAttention({ api, state, open, openStep, toast, renderBadges }) {
  let items = [],
    statuses = {},
    generation = 0,
    signature = '';
  const area = $('attention-list');
  const kinds = {
    question: '等你回答',
    approval: '待审批',
    science: '需核对',
    submission: '需核对',
    uncertain: '需核对',
  };
  function draw() {
    area.hidden = !items.length;
    $('attention-count').textContent = String(items.length);
    $('attention-items').innerHTML = items
      .map(
        (item, index) => `<button type="button" data-attention-index="${index}">
      <span data-no-i18n>${esc([item.title, item.detail].filter(Boolean).join(' · '))}</span><small>${t(kinds[item.kind])}</small></button>`,
      )
      .join('');
  }
  async function refresh() {
    if (document.hidden || (state.auth.enabled && !state.auth.user)) return;
    const token = ++generation,
      version = state.accountVersion;
    try {
      const data = await api('/api/chat/attention');
      if (token !== generation || version !== state.accountVersion) return;
      const next = JSON.stringify(data);
      if (next === signature) return;
      signature = next;
      items = data.items;
      statuses = data.statuses || {};
      draw();
      renderBadges();
    } catch (error) {
      if (area.open && token === generation) toast(error.message);
    }
  }
  $('attention-items').onclick = async (e) => {
    const trigger = e.target.closest('[data-attention-index]');
    if (!trigger || state.busy) return;
    const item = items[Number(trigger.dataset.attentionIndex)];
    await open(item.session);
    if (state.sessionId !== item.session) return;
    try {
      if (item.plan) {
        await openStep(item);
        return;
      }
      const selector =
        item.kind === 'approval'
          ? `[data-approval-id="${CSS.escape(item.id)}"]`
          : item.kind === 'question'
            ? `[data-question="${CSS.escape(item.id)}"]`
            : `[data-receipt="${CSS.escape(item.id)}"]`;
      const element = document.querySelector(selector);
      if (element && !element.hidden) {
        element.scrollIntoView({ block: 'center' });
        element.querySelector('button,textarea,input')?.focus({ preventScroll: true });
      } else {
        toast(t('事项已更新，请查看当前对话。'));
        void refresh();
      }
    } catch (error) {
      toast(error.message);
    }
  };
  window.addEventListener('focus', refresh);
  area.addEventListener('toggle', () => {
    if (area.open) void refresh();
  });
  setInterval(refresh, 20000);
  return {
    refresh,
    badge(id) {
      const item = items.find((row) => row.session === id);
      return item ? t(kinds[item.kind]) : statuses[id] === 'completed' ? t('本轮完成') : '';
    },
    reset() {
      generation++;
      items = [];
      statuses = {};
      signature = '';
      draw();
    },
  };
}
