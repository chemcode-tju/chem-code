import {escapeHtml as escape} from './core.mjs';
import {fileIcon} from './file-actions.mjs';
import {getFileBlob, isStructureName, isStreamableStructure} from './structure-links.mjs';
import {t} from './i18n.mjs';
import {fileMessage as message, renderFilePreview} from './file-preview.mjs';

function attachmentUrl(id, name) {
  const params = new URLSearchParams({source:`attachment:${id}`, path:name});
  return operation => `/api/file-browser/${operation}?${params}`;
}

// Adapted from OpenCode v2.2.0's file/list.tsx. See vendor/opencode/LICENSE.
function applyFileListKeyDown(event, buttons, highlighted, {onHighlight, onSelect}) {
  if (!buttons.length) return;
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    const currentIndex = buttons.indexOf(highlighted);
    const delta = event.key === 'ArrowDown' ? 1 : -1;
    const start = currentIndex === -1 ? (delta > 0 ? 0 : buttons.length - 1) : currentIndex + delta;
    const index = Math.max(0, Math.min(buttons.length - 1, start));
    onHighlight(buttons[index]);
    event.preventDefault();
    return;
  }
  if (event.key !== 'Enter') return;
  onSelect(highlighted ?? buttons[0]);
  event.preventDefault();
}

export function createFileBrowser({api, session, viewer}) {
  const byId = id => document.getElementById(id);
  const layout = document.querySelector('.file-browser-layout');
  const sourceMenu = byId('file-browser-source');
  const rootForm = byId('file-browser-root');
  const pathInput = byId('file-browser-path');
  const tree = byId('file-browser-tree');
  const filterInput = byId('file-browser-filter');
  const results = byId('file-browser-results');
  const backButton = byId('file-preview-back');
  const previewBody = byId('file-preview-body');
  const previewHeader = byId('file-preview-header');
  const previewActions = byId('file-preview-actions');
  const roots = new Map();
  let sources = [], workspace = session(), root = '', treeRequest, sourceRequest, previewRequest;
  let searchRequest, searchTimer;
  let selected = null, directoryId = 0;
  let previewAttachment = false;
  let project = null;
  let followingProject = false;

  function url(operation, {path = '', offset = 0, query = ''} = {}) {
    const source = sourceMenu.value;
    const params = new URLSearchParams({source, path});
    if (source === 'workspace' && workspace) params.set('session', workspace);
    else if (root) params.set('root', root);
    if (offset) params.set('offset', offset);
    if (query) params.set('q', query);
    return `/api/file-browser/${operation}?${params}`;
  }

  function clearPreview() {
    previewRequest?.abort();
    previewAttachment = false;
    previewHeader.hidden = true;
    layout.dataset.view = 'tree';
    previewBody.innerHTML = `<div class="file-preview-empty">${fileIcon('folder-open')}<strong>打开文件</strong><p class="file-browser-message">选择要预览的文件</p></div>`;
  }

  function structureAction(path, name) {
    return `<button type="button" class="file-action" data-file-structure="${escape(path)}" data-filename="${escape(name)}" title="查看结构" aria-label="${escape(t('查看结构：{0}').replace('{0}', () => name))}">${fileIcon('eye')}</button>`;
  }

  function row(entry, parent) {
    const {name, kind} = entry;
    const path = entry.path ?? (parent ? `${parent}/${name}` : name);
    const directory = entry.path?.includes('/') ? entry.path.slice(0, entry.path.lastIndexOf('/')) : '';
    const folder = kind === 'directory';
    const group = folder ? `file-children-${++directoryId}` : '';
    const openAttrs = folder ? ` aria-controls="${group}" aria-expanded="false" data-directory` :
      ` data-file ${path === selected ? 'aria-current="true"' : ''}`;
    return `<li><div class="file-browser-row">
      <button type="button" class="file-browser-entry" data-entry="${escape(path)}"${openAttrs}>
        ${folder ? fileIcon('chevron') : '<span class="file-tree-indent" aria-hidden="true"></span>'}
        ${fileIcon(folder ? 'folder' : 'document')}<span class="file-browser-name" title="${escape(path)}" data-no-i18n><span>${escape(name)}</span>${directory ? `<small>${escape(directory)}</small>` : ''}</span>
      </button>${!folder && isStructureName(name) ? `<span class="workspace-file-actions">${structureAction(path, name)}</span>` : ''}
    </div>${folder ? `<div class="file-tree-children" id="${group}" hidden></div>` : ''}</li>`;
  }

  async function loadDirectory(target, path = '', offset = 0) {
    const signal = treeRequest.signal;
    const submittedPath = pathInput.value;
    const more = target.querySelector(':scope > .file-tree-more');
    if (more) more.disabled = true;
    if (!offset) message(target, '正在读取目录…');
    try {
      const data = await api(url('directory', {path, offset}), {signal});
      if (signal.aborted || !target.isConnected) return;
      if (!path && data.root) {
        root = data.root;
        roots.set(sourceMenu.value, root);
        if (pathInput.value === submittedPath) pathInput.value = root;
        pathInput.title = root;
      }
      if (!offset) target.replaceChildren();
      more?.remove();
      if (!offset && !data.entries.length) message(target, '目录为空');
      else {
        let list = target.querySelector(':scope > ul');
        if (!list) {
          list = document.createElement('ul');
          target.append(list);
        }
        const index = list.children.length;
        list.insertAdjacentHTML('beforeend', data.entries.map(entry => row(entry, path)).join(''));
        if (offset) list.children[index]?.querySelector('button').focus({preventScroll:true});
      }
      if (data.next_offset != null) addPageButton(target, path, data.next_offset, '加载更多文件');
      if (!path && !offset) {
        filterInput.disabled = false;
        if (filterInput.value.trim()) updateFilter();
      }
    } catch (error) {
      if (signal.aborted || !target.isConnected) return;
      if (!path && !offset) clearFilter();
      if (!offset) message(target, error.message, 'alert');
      else {
        more?.remove();
        const status = document.createElement('p');
        status.className = 'file-browser-message';
        status.setAttribute('role', 'alert');
        status.textContent = t(error.message);
        target.querySelector(':scope > [role=alert]')?.remove();
        target.append(status);
      }
      addPageButton(target, path, offset, '重试');
    }
  }

  function addPageButton(target, path, offset, label) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'file-tree-more';
    button.textContent = t(label);
    button.onclick = () => {
      target.querySelector(':scope > [role=alert]')?.remove();
      void loadDirectory(target, path, offset);
    };
    target.append(button);
  }

  function reload() {
    treeRequest?.abort();
    treeRequest = new AbortController();
    directoryId = 0;
    rootForm.hidden = !sourceMenu.value || sourceMenu.value === 'workspace';
    filterInput.disabled = true;
    if (!sourceMenu.value) return;
    if (sourceMenu.value === 'workspace' && !workspace) message(tree, '请先打开一个对话。');
    else void loadDirectory(tree);
  }

  async function loadSources() {
    sourceRequest?.abort();
    sourceRequest = new AbortController();
    const {signal} = sourceRequest;
    if (!sources.length) message(tree, '正在读取文件位置…');
    try {
      const result = await api('/api/file-browser/sources', {signal});
      if (signal.aborted) return;
      if (JSON.stringify(result.sources) === JSON.stringify(sources)) return;
      const previous = sourceMenu.value;
      sources = result.sources;
      sourceMenu.replaceChildren(...sources.map(({id, label}) => {
        const option = new Option(id.startsWith('ssh:') ? label : t(label), id);
        if (id.startsWith('ssh:')) option.setAttribute('data-no-i18n', '');
        return option;
      }));
      sourceMenu.disabled = false;
      const preferred = project?.source;
      sourceMenu.value = sources.some(item => item.id === previous) ? previous :
        (sources.some(item => item.id === preferred) ? preferred :
          !workspace && sources.some(item => item.id === 'local') ? 'local' : 'workspace');
      if (sourceMenu.value !== previous) followingProject = sourceMenu.value === preferred;
      if (sourceMenu.value !== previous) changeSource(!previous && previewAttachment);
    } catch (error) {
      if (signal.aborted) return;
      if (!sources.length) message(tree, error.message, 'alert');
    }
  }

  function changeSource(preservePreview = false) {
    selected = null;
    clearFilter();
    if (!preservePreview) clearPreview();
    root = roots.get(sourceMenu.value) || '';
    pathInput.value = root;
    pathInput.title = root;
    reload();
  }

  function updateFilter() {
    clearTimeout(searchTimer);
    searchRequest?.abort();
    const query = filterInput.value.trim();
    tree.hidden = Boolean(query);
    results.hidden = !query;
    results.replaceChildren();
    if (!query) return;
    searchRequest = new AbortController();
    const {signal} = searchRequest;
    message(results, '正在查找文件…');
    searchTimer = setTimeout(async () => {
      try {
        const data = await api(url('search', {query}), {signal});
        if (signal.aborted) return;
        results.replaceChildren();
        if (data.files.length) {
          const list = document.createElement('ul');
          list.innerHTML = data.files.map(entry => row(entry, '')).join('');
          results.append(list);
        } else message(results, '没有匹配的文件');
        if (data.limited) {
          const note = document.createElement('p');
          note.className = 'file-browser-message';
          note.textContent = t('搜索范围较大，请缩小目录或筛选条件。');
          results.append(note);
        }
      } catch (error) {
        if (!signal.aborted) message(results, error.message, 'alert');
      }
    }, 240);
  }

  function clearFilter() {
    filterInput.value = '';
    updateFilter();
  }

  function selectEntry(button) {
    selected = button.dataset.entry;
    for (const entry of layout.querySelectorAll('[data-entry]')) {
      if (entry.dataset.entry === selected) entry.setAttribute('aria-current', 'true');
      else entry.removeAttribute('aria-current');
    }
  }

  function openPreview(button) {
    const path = button.dataset.entry, name = path.split('/').at(-1);
    selectEntry(button);
    const filePath = root ? `${root.replace(/\/$/, '')}/${path}` : path;
    const location = sourceMenu.value.startsWith('ssh:') ? `${sourceMenu.selectedOptions[0].textContent} · ${filePath}` : filePath;
    return showPreview(name, location, operation => url(operation,{path}),
      isStructureName(name) ? structureAction(path, name) : '');
  }

  async function showPreview(name, location, endpoint, structure, attachment = false) {
    clearPreview();
    previewAttachment = attachment;
    previewRequest = new AbortController();
    const {signal} = previewRequest;
    for (const [id, text] of [['file-preview-name', name], ['file-preview-path', location]]) {
      byId(id).textContent = text;
      byId(id).title = text;
    }
    previewActions.innerHTML = structure;
    previewHeader.hidden = false;
    message(previewBody, '正在读取文件…');
    layout.dataset.view = 'preview';
    if (backButton.getClientRects().length) backButton.focus({preventScroll:true});
    try {
      await renderFilePreview({
        api, name, target:previewBody, actions:previewActions, signal,
        previewUrl:endpoint('preview'), contentUrl:endpoint('content'), mediaUrl:endpoint('media'),
      });
    } catch (error) {
      if (!signal.aborted) message(previewBody, error.message, 'alert');
    }
  }

  sourceMenu.onchange = () => { followingProject = false; changeSource(); };
  byId('file-browser-refresh').onclick = () => {
    clearTimeout(searchTimer);
    searchRequest?.abort();
    if (sourceMenu.value) reload();
    void loadSources();
  };
  rootForm.onsubmit = event => {
    event.preventDefault();
    followingProject = false;
    root = pathInput.value.trim();
    selected = null;
    clearFilter();
    clearPreview();
    reload();
  };
  backButton.onclick = () => {
    layout.dataset.view = 'tree';
    const visibleList = results.hidden ? tree : results;
    (visibleList.querySelector('[aria-current]') || filterInput).focus({preventScroll:true});
  };
  filterInput.addEventListener('input', updateFilter);
  filterInput.addEventListener('keydown', event => {
    if (event.key === 'Escape' && filterInput.value) {
      event.preventDefault();
      clearFilter();
      return;
    }
    applyFileListKeyDown(event, [...results.querySelectorAll('[data-entry]')], results.querySelector('[data-highlighted]'), {
      onHighlight(button) {
        results.querySelector('[data-highlighted]')?.removeAttribute('data-highlighted');
        button.setAttribute('data-highlighted', '');
        button.scrollIntoView({block:'nearest'});
      },
      onSelect: button => void openPreview(button),
    });
  });
  layout.addEventListener('click', event => {
    const eye = event.target.closest('[data-file-structure]');
    if (eye) {
      const href = url('content', {path:eye.dataset.fileStructure});
      const entry = eye.closest('.file-browser-row')?.querySelector('[data-entry]');
      if (entry) selectEntry(entry);
      const source = sourceMenu.selectedOptions[0].textContent;
      const path = root ? `${root.replace(/\/$/, '')}/${eye.dataset.fileStructure}` : eye.dataset.fileStructure;
      const {filename:name} = eye.dataset;
      const trajectory = isStreamableStructure(name) ? {name, url:url('trajectory', {path:eye.dataset.fileStructure})} : null;
      viewer.open(name, ({signal}) => getFileBlob(href, {signal}), {key:href, title:`${source} · ${path}`, trajectory});
      return;
    }
    const entry = event.target.closest('[data-entry]');
    if (!entry) return;
    if (entry.hasAttribute('data-file')) {
      void openPreview(entry);
      return;
    }
    const children = byId(entry.getAttribute('aria-controls'));
    children.hidden = !children.hidden;
    entry.setAttribute('aria-expanded', String(!children.hidden));
    if (!children.hidden && !children.hasChildNodes()) void loadDirectory(children, entry.dataset.entry);
  });
  tree.addEventListener('keydown', event => {
    const entry = event.target.closest('[data-entry]');
    if (!entry) return;
    const entries = [...tree.querySelectorAll('[data-entry]')].filter(button => button.getClientRects().length);
    const index = entries.indexOf(entry);
    const expanded = entry.getAttribute('aria-expanded');
    let target;
    if (event.key === 'ArrowDown') target = entries[index + 1];
    if (event.key === 'ArrowUp') target = entries[index - 1];
    if (event.key === 'ArrowRight') {
      if (expanded === 'false') { event.preventDefault(); entry.click(); }
      if (expanded === 'true') target = byId(entry.getAttribute('aria-controls')).querySelector('[data-entry]');
    }
    if (event.key === 'ArrowLeft') {
      if (expanded === 'true') { event.preventDefault(); entry.click(); }
      else target = entry.closest('.file-tree-children')?.previousElementSibling?.querySelector('[data-entry]');
    }
    if (target) { event.preventDefault(); target.focus(); }
  });
  clearPreview();

  return {
    activate() { void loadSources(); },
    syncProject(location) {
      const source = !location?.host_id || location.host_id === 'local' ? 'local' : `ssh:${location.host_id}`;
      const path = location?.directory;
      if (!path) {
        if (followingProject && project && sources.length) {
          roots.delete(project.source);
          sourceMenu.value = workspace ? 'workspace' : 'local';
          changeSource();
        }
        followingProject = false;
        project = null;
        return;
      }
      if (project?.source === source && project.path === path) return;
      project = {source,path};
      roots.set(source,path);
      if (!sources.some(item=>item.id===source)) return;
      followingProject = true;
      sourceMenu.value = source;
      changeSource();
    },
    openAttachment(id, name) {
      return showPreview(name,name,attachmentUrl(id,name),'',true);
    },
    openAttachmentStructure(id, name) {
      const endpoint = attachmentUrl(id,name), href = endpoint('content');
      const trajectory = isStreamableStructure(name) ? {name, url:endpoint('trajectory')} : null;
      viewer.open(name, options => getFileBlob(href,options), {key:href,title:name,trajectory});
    },
    syncSession() {
      if (workspace === session()) return;
      workspace = session();
      if (previewAttachment) clearPreview();
      if (sourceMenu.value === 'workspace') changeSource();
    },
    reset() {
      sourceRequest?.abort();
      treeRequest?.abort();
      clearFilter();
      filterInput.disabled = true;
      clearPreview();
      workspace = session();
      project = null;
      followingProject = false;
      roots.clear();
      sources = [];
      sourceMenu.replaceChildren();
      sourceMenu.disabled = true;
      rootForm.hidden = true;
      root = '';
      selected = null;
      pathInput.value = '';
      tree.replaceChildren();
    },
  };
}
