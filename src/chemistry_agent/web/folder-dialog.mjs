import {t} from './i18n.mjs';
import {showSlowDirectoryRead} from './file-preview.mjs';

const icon = name => `<svg class="icon" aria-hidden="true"><use href="/assets/ui-icons.svg#i-${name}"/></svg>`;

export function createFolderDialog(api, trigger, applyFolder) {
  const dialog = document.getElementById(trigger.getAttribute('aria-controls'));
  const $ = name => dialog.querySelector(`[data-folder-${name}]`);
  const input = $('path');
  const list = $('list');
  const create = $('create-form');
  const status = $('status');
  let scope, listing, selected, busy = false, generation = 0;

  function endpoint() {
    return `/api/hosting/hosts/${encodeURIComponent(scope.host)}/directories`;
  }

  function post(path, body) {
    return api(path, {method:'POST', body:JSON.stringify(body)});
  }

  function lookup(path, children = true) {
    return api(`${endpoint()}?${new URLSearchParams({path:path.trim(),children})}`);
  }

  function notify(message = '', error = false) {
    status.textContent = t(message);
    status.classList.toggle('error', error);
  }

  function syncControls() {
    dialog.setAttribute('aria-busy', String(busy));
    input.disabled = busy;
    list.querySelectorAll('button').forEach(button => { button.disabled = busy; });
    $('go').disabled = $('home').disabled = $('clear').disabled = busy;
    $('up').disabled = busy || !listing || listing.parent === listing.path;
    $('new').disabled = busy || !listing?.exists || input.value !== listing.path || !create.hidden;
    $('create-name').disabled = $('create-save').disabled = busy;
    $('select').disabled = busy || !input.value.trim();
  }

  async function run(work) {
    if (busy || !scope || !dialog.open) return;
    const version = ++generation;
    const active = () => version === generation && dialog.open;
    busy = true;
    syncControls();
    try { await work(active); }
    catch (error) { if (active()) notify(error.message, true); }
    finally {
      if (active()) {
        busy = false;
        syncControls();
      }
    }
  }

  function render(result) {
    listing = result;
    selected = null;
    input.value = result.path;
    input.title = result.path;
    create.hidden = true;
    list.replaceChildren(...result.directories.map(entry => {
      const row = document.createElement('li');
      const name = document.createElement('button');
      name.type = 'button';
      name.className = 'folder-item';
      name.title = entry.path;
      name.setAttribute('aria-pressed', 'false');
      name.innerHTML = icon('folder');
      const label = document.createElement('span');
      label.textContent = entry.name;
      label.dataset.noI18n = '';
      name.append(label);
      name.onclick = () => {
        selected = entry.path;
        for (const item of list.querySelectorAll('.folder-item')) {
          item.setAttribute('aria-pressed', String(item === name));
        }
      };
      name.ondblclick = () => load(entry.path);
      name.onkeydown = event => {
        if (event.key === 'ArrowRight') { event.preventDefault(); void load(entry.path); }
      };
      const open = document.createElement('button');
      open.type = 'button';
      open.className = 'icon-button toolbar-button';
      open.setAttribute('aria-label', `${t('打开目录')} · ${entry.name}`);
      open.innerHTML = icon('chevron');
      open.onclick = () => load(entry.path);
      row.append(name, open);
      return row;
    }));
    list.scrollTop = 0;
    $('empty').hidden = !result.exists || Boolean(result.directories.length);
    notify(!result.exists ? '首次使用时创建此文件夹。' : result.truncated ? '只显示部分文件夹，可直接输入完整路径。' : '');
  }

  function load(path, message = '') {
    return run(async active => {
      notify();
      const cancelNotice = showSlowDirectoryRead(() => { if (active()) notify('正在读取目录…'); });
      try {
        const result = await lookup(path || '~/');
        if (active()) {
          render(result);
          if (message) notify(message, true);
        }
      } finally {
        cancelNotice();
      }
    });
  }

  async function commit(path, active) {
    if (!active()) return;
    if (await applyFolder(path) && active()) close();
  }

  function close() {
    generation++;
    scope = null;
    busy = false;
    if (dialog.open) dialog.close();
  }

  $('path-form').onsubmit = event => { event.preventDefault(); void load(input.value); };
  input.oninput = () => {
    selected = null;
    for (const item of list.querySelectorAll('.folder-item[aria-pressed=true]')) item.setAttribute('aria-pressed', 'false');
    syncControls();
  };
  $('up').onclick = () => load(listing.parent);
  $('home').onclick = () => load('~/');
  $('select').onclick = () => run(async active => {
    const chosen = input.value === listing?.path && selected ? selected : input.value;
    const result = await lookup(chosen,false);
    await commit(result.path, active);
  });
  $('clear').onclick = () => run(active => commit('', active));
  $('new').onclick = () => {
    create.reset();
    create.hidden = false;
    syncControls();
    $('create-name').focus();
  };
  $('create-cancel').onclick = () => {
    create.hidden = true;
    syncControls();
    $('new').focus();
  };
  create.onsubmit = event => {
    event.preventDefault();
    const name = $('create-name').value.trim();
    if (!name) { notify('请输入文件夹名称。', true); return; }
    void run(async active => {
      const result = await post(endpoint(), {parent:listing.path, name});
      if (!active()) return;
      create.hidden = true;
      // The directory has already been created. Reflect it even if relisting fails.
      render({path:result.path, parent:listing.path, exists:true, directories:[]});
    });
  };
  $('close').onclick = close;
  dialog.addEventListener('cancel', event => { event.preventDefault(); close(); });
  dialog.addEventListener('close', () => {
    if (!dialog.open) {
      trigger.setAttribute('aria-expanded', 'false');
      if (!trigger.disabled) trigger.focus({preventScroll:true});
    }
  });

  return {
    close,
    open({host, label, directory, message}) {
      if (dialog.open) return;
      scope = {host};
      listing = null;
      selected = null;
      list.replaceChildren();
      input.value = directory || '~/';
      input.title = input.value;
      $('host').textContent = label;
      $('empty').hidden = create.hidden = true;
      notify();
      dialog.showModal();
      trigger.setAttribute('aria-expanded', 'true');
      void load(input.value,message);
    },
  };
}
