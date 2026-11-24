import {createAnchoredPanel} from './agent-menu.mjs';
import {presetDescription} from './task-agent-control.mjs';
import {t} from './i18n.mjs';

export function createGroupPicker(anchor, {api, current, save, settings}) {
  const panel = document.createElement('section');
  panel.className = 'agent-menu group-picker';
  panel.id = 'conversation-group-panel';
  panel.setAttribute('role', 'dialog');
  panel.innerHTML = `<h2>群组成员</h2>
    <label><span>协作方式</span><select data-strategy aria-label="协作方式">
      <option value="handoff">按需交接</option><option value="ordered">依次处理</option>
    </select></label><div data-members></div>
    <button type="button" data-add>添加成员</button>
    <p role="status"></p><footer class="model-picker-footer">
    <button type="button" data-settings>配置预设…</button><button type="button" data-off>关闭群组</button>
    <button type="button" data-save>保存群组</button></footer>`;
  const popup = createAnchoredPanel(anchor, panel);
  const rows = panel.querySelector('[data-members]'), status = panel.querySelector('[role=status]');
  const add = panel.querySelector('[data-add]'), apply = panel.querySelector('[data-save]');
  const off = panel.querySelector('[data-off]');
  const strategy = panel.querySelector('[data-strategy]');
  let options = [], draft = [], generation = 0, pending = false;
  const key = item => item ? item.id + ':' + item.version : '';
  function draw() {
    strategy.disabled = pending;
    rows.replaceChildren();
    draft.forEach((member, index) => {
      const line = document.createElement('label'), title = document.createElement('span');
      title.textContent = strategy.value === 'ordered' ? t('发言顺序') + ' ' + (index + 1)
        : index === 0 ? t('默认接手') : t('协作成员');
      const select = document.createElement('select');
      select.setAttribute('aria-label', title.textContent);
      select.disabled = pending;
      select.add(new Option(t('选择预设'), ''));
      if (member && !options.some(item=>key(item)===key(member))) {
        const previous = new Option(member.name + ' · v' + member.version, key(member));
        previous.disabled = true; select.add(previous);
      }
      for (const item of options) {
        const choice = new Option(item.name + ' · ' + presetDescription(item), key(item));
        choice.dataset.noI18n = '';
        select.add(choice);
      }
      select.value = key(member);
      select.onchange = () => {
        const next = options.find(item => key(item) === select.value);
        const existing = next ? draft.findIndex(item => item?.id === next.id) : -1;
        if (existing >= 0 && existing !== index) draft[existing] = member;
        draft[index] = next;
        if (!next && draft.length > 2) draft.splice(index, 1);
        draw(); rows.querySelectorAll('select')[Math.min(index, draft.length-1)].focus();
      };
      line.append(title, select); rows.append(line);
    });
    add.disabled = pending || draft.includes(undefined) || draft.length >= Math.min(8, options.length);
    apply.disabled = pending || draft.some(item => !item || !options.some(row => key(row) === key(item)));
    off.hidden = !current().length; off.disabled = pending;
  }
  add.onclick = () => {draft.push(undefined); draw(); rows.lastChild.querySelector('select').focus();};
  panel.querySelector('[data-settings]').onclick = () => {popup.close(); settings();};
  async function commit(value) {
    if (pending) return;
    const version = generation;
    pending = true; draw();
    try {if (await save(value) && version === generation) popup.close(true);}
    catch(error) {if (version === generation) status.textContent = error.message;}
    finally {if (version === generation) {pending = false; draw();}}
  }
  strategy.onchange = draw;
  apply.onclick = () => commit(draft.map(member=>({...member,strategy:strategy.value})));
  off.onclick = () => commit([]);
  return {
    close() {generation++; pending = false; popup.close();},
    get visible() {return popup.visible;},
    async open() {
      anchor.setAttribute('aria-controls', panel.id);
      const version = ++generation;
      strategy.value = current().length ? (current()[0].strategy || 'ordered') : 'handoff';
      draft = current().length ? current().slice() : [undefined, undefined];
      options = []; pending = false; draw();
      panel.setAttribute('aria-label',t('群组成员')); status.textContent = t('正在读取预设…');
      popup.open(); rows.querySelector('select').focus();
      try {
        const data = await api('/api/agents/presets');
        if (version !== generation || !popup.visible) return;
        options = data.items.filter(item => item.enabled);
        draw();
        status.textContent = options.length < 2 ? t('请先在 Agent 设置中添加至少两个执行预设。') : '';
        rows.querySelector('select').focus();
      } catch(error) {if (version === generation) status.textContent = error.message;}
    },
  };
}
