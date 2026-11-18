import {escapeHtml as esc} from './core.mjs';
import {fileIcon} from './file-actions.mjs';
import {fileMessage, renderFilePreview} from './file-preview.mjs';
import {createStructureBridge, getFileBlob, isStructureName, isStreamableStructure} from './structure-links.mjs';

export function initJobMonitor() {
  const $ = id => document.getElementById(id);
  const hostSelect = $('queue-host');
  const jobList = $('queue-jobs');
  const detail = $('queue-detail');
  const frame = $('job-viewer-frame');
  const stateLabels = {
    RUNNING:'运行中', PENDING:'排队中', CONFIGURING:'配置中', COMPLETING:'收尾中',
    SUSPENDED:'已挂起', STOPPED:'已暂停', RESIZING:'调整中', STAGE_OUT:'正在传出',
    COMPLETED:'已完成', FAILED:'失败', CANCELLED:'已取消', TIMEOUT:'超时',
    OUT_OF_MEMORY:'内存不足', NODE_FAIL:'节点故障', PREEMPTED:'已抢占',
  };
  const label = job => stateLabels[job.status] || job.status;
  const key = job => [job.host, job.id, job.submitted].join(':');
  const converterLimit = 32 * 1024 ** 2;
  const isDump = filename => /\.(?:lammpstrj|dump)$/i.test(filename);
  const canPreview = file => isStructureName(file.name) &&
    (isStreamableStructure(file.name) || file.size <= converterLimit);
  function shortPath(path, home) {
    if (!path || !path.startsWith('/')) return '';
    if (home && (path === home || path.startsWith(home + '/'))) return '~' + path.slice(home.length);
    return path;
  }
  const structureOrder = [
    /^CONTCAR(?:$|[._-])|\.contcar$/i,
    /^(?:final|relaxed|optimized)[._-]/i,
    /^(?:POSCAR|XDATCAR)(?:$|[._-])/i,
    /\.(?:extxyz|xyz|vasp|cif|gro|pdb|data)$/i,
  ];
  let results = [], selected = null, directory = '', reading = 0, refreshing = false;
  let folderEntries = [], activePreview = '', history = false, refreshGeneration = 0;
  let visible = false, loaded = false, resourceRequest, textRequest;

  async function api(url, options) {
    const response = await fetch(url, {cache:'no-store', ...options});
    const data = await response.json();
    if (!response.ok) throw Error(data.error?.message || '未能读取作业信息。');
    return data;
  }

  const jobURL = (job, endpoint, path, options = {}) => {
    const params = new URLSearchParams({submitted:job.submitted,path,history:job.history,...options});
    return `/api/job-monitor/${encodeURIComponent(job.host)}/jobs/${encodeURIComponent(job.id)}/${endpoint}?${params}`;
  };

  const fileURL = (job, operation, path, options = {}) => {
    const params = new URLSearchParams({source:`job:${job.host}`,job_id:job.id,
      submitted:job.submitted,path,history:String(job.history),...options});
    return `/api/file-browser/${operation}?${params}`;
  };

  function drawQueue(preserveFocus = true) {
    const visible = results.filter(row => !hostSelect.value || row.id === hostSelect.value);
    const jobs = visible.flatMap(row => row.jobs || []);
    const running = jobs.filter(job => job.status === 'RUNNING').length;
    const pending = jobs.filter(job => job.status === 'PENDING').length;
    $('queue-count').textContent = history
      ? `最近 7 天 · ${jobs.length} 项历史作业${visible.some(row => row.truncated) ? ' · 每台最多 300 项' : ''}`
      : `${jobs.length} 项作业 · ${running} 运行中 · ${pending} 排队中`;
    $('queue-errors').innerHTML = visible.filter(row => row.error).map(row =>
      `<p class="queue-message" role="status">${esc(row.name)} · ${esc(row.error)}</p>`).join('');
    const focus = preserveFocus ? document.activeElement.dataset.job : null;
    const markup = visible.filter(row => !row.error).map(row => `
      <section class="queue-host">
        <h2>${fileIcon('servers')}${esc(row.name)} <small>${row.jobs.length}</small></h2>
        ${row.partitions?.length ? `<div class="queue-partitions" aria-label="服务器分区">${row.partitions.map(partition => `<span>${esc(partition)}</span>`).join('')}</div>` : ''}
        ${row.partition_error ? `<p class="queue-message">${esc(row.partition_error)}</p>` : ''}
        ${row.jobs.length ? `<div class="queue-row queue-labels" aria-hidden="true"><span>作业</span><span>状态</span><span>分区</span><span class="queue-elapsed">运行时间</span></div>
        ${row.jobs.map(job => `<div class="queue-job"><button type="button" class="queue-row" data-job="${esc(key(job))}" aria-controls="queue-detail" aria-expanded="${selected && key(selected) === key(job) ? 'true' : 'false'}">
          <span title="${esc(job.name)}">${esc(job.name)}<small>#${esc(job.id)}</small></span>
          <span class="queue-state" data-state="${esc(job.status)}" title="${esc(job.status)}">${esc(label(job))}</span>
          <span class="queue-partition" title="${esc(job.partition)}">${esc(job.partition)}</span>
          <span class="queue-elapsed">${esc(job.elapsed)}</span>
        </button>${shortPath(job.directory,job.home) ? `<code class="queue-path" tabindex="0" aria-label="作业目录" title="${esc(job.directory)}">${esc(shortPath(job.directory,job.home))}</code>` : ''}</div>`).join('')}` : `<p class="queue-message">${history ? '最近 7 天暂无历史作业' : '队列中暂无作业'}</p>`}
      </section>`).join('');
    if (jobList.dataset.markup !== markup) {
      jobList.innerHTML = markup || (results.length ? '' : '<p class="queue-message">请先在设置中启用计算服务器。 <a href="/settings#servers">服务器与环境</a></p>');
      jobList.dataset.markup = markup;
      if (focus) [...jobList.querySelectorAll('[data-job]')].find(button => button.dataset.job === focus)?.focus();
    }
  }

  async function refresh() {
    if (refreshing) return;
    refreshing = true;
    const generation = ++refreshGeneration;
    const requestedHistory = history;
    $('queue-refresh').disabled = true;
    try {
      const {hosts} = await api('/api/job-monitor/hosts');
      if (generation !== refreshGeneration) return;
      const options = '<option value="">全部服务器</option>' + hosts.map(host =>
        `<option value="${esc(host.id)}">${esc(host.name)}</option>`).join('');
      if (options !== hostSelect.dataset.options) {
        const current = hostSelect.value;
        hostSelect.innerHTML = options;
        hostSelect.dataset.options = options;
        hostSelect.value = hosts.some(host => host.id === current) ? current : '';
      }
      const fetched = await Promise.all(hosts.map(async host => {
        try {
          const data = await api(`/api/job-monitor/${encodeURIComponent(host.id)}/jobs?history=${requestedHistory}`);
          return {...host, partitions:data.partitions, partition_error:data.partition_error,
            truncated:data.truncated,
            jobs:data.jobs.map(job => ({...job,host:host.id,home:data.home,history:requestedHistory}))};
        } catch(error) { return {...host,error:error.message}; }
      }));
      if (generation !== refreshGeneration) return;
      results = fetched;
      loaded = true;
      if (selected) {
        const host = results.find(row => row.id === selected.host);
        const job = host?.jobs?.find(job => key(job) === key(selected));
        if (job) {
          const moved = job.directory !== selected.directory;
          selected = job;
          if (moved) void loadFiles('');
          renderJob();
        } else if (!host?.error) {
          $('job-meta').textContent = `#${selected.id} · 已离开队列`;
        }
      }
      drawQueue();
      $('queue-updated').textContent = new Date().toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
    } catch(error) {
      if (generation !== refreshGeneration) return;
      $('queue-count').textContent = history ? '未能读取历史作业' : '未能读取队列';
      $('queue-errors').textContent = error.message;
    } finally {
      if (generation === refreshGeneration) {
        $('queue-refresh').disabled = false;
        refreshing = false;
      }
    }
  }

  function renderJob() {
    $('job-heading').textContent = selected.name;
    $('job-heading').title = selected.name;
    const host = results.find(row => row.id === selected.host);
    $('job-meta').textContent = [host?.name || selected.host, `#${selected.id}`, label(selected), selected.partition].join(' · ');
  }

  function defaultStructure(entries) {
    const candidates = entries.filter(entry => !entry.directory && entry.size !== 0 && canPreview(entry));
    for (const pattern of structureOrder) {
      const entry = candidates.find(file => pattern.test(file.name));
      if (entry) return entry.name;
    }
    return candidates[0]?.name;
  }

  function fileStatus(text) {
    $('job-file-status').textContent = text;
    $('job-file-status').hidden = !text;
  }

  function folderPath() {
    return [selected.directory, directory].filter(Boolean).join('/');
  }

  function sizeLabel(bytes) {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KiB`;
    return `${(bytes / 1024 ** 2).toFixed(1)} MiB`;
  }

  async function loadFiles(path, autoPreview = true) {
    if (!selected) return;
    const request = ++reading;
    directory = path;
    closeViewer();
    $('job-parent').hidden = !path;
    $('job-path').title = folderPath();
    $('job-path').textContent = shortPath(folderPath(), selected.home) || '目录不可用';
    folderEntries = [];
    $('job-files').replaceChildren();
    fileStatus('正在读取目录…');
    try {
      const job=selected;
      const {entries,next_offset} = await api(fileURL(job,'directory',path));
      if (request !== reading) return;
      folderEntries = [];
      appendFiles(entries,next_offset,job,path,request);
      const name = autoPreview && defaultStructure(folderEntries);
      if (name) void openPreview(name);
    } catch(error) {
      if (request === reading) fileStatus(error.message);
    }
  }

  function appendFiles(entries,next,job,path,request) {
    const page = entries.map(entry=>({...entry,directory:entry.kind==='directory'}));
    folderEntries.push(...page);
    $('job-files').insertAdjacentHTML('beforeend',page.map(entry => {
        const name = esc(entry.name);
        if (entry.directory) {
          return `<div class="queue-file"><button type="button" class="queue-file-name" data-folder="${name}" title="${name}">${fileIcon('folder')}<span>${name}</span></button></div>`;
        }
        const available = canPreview(entry) && entry.size !== 0;
        const title = available ? '查看结构' : entry.size > converterLimit ? '该格式超过单文件转换上限' : '文件为空';
        const preview = isStructureName(entry.name) ? `<button type="button" class="icon-button" data-preview="${name}" title="${title}" aria-label="${title}：${name}" ${available ? '' : 'disabled'}>${fileIcon('eye')}</button>` : '';
        return `<div class="queue-file"><button type="button" class="queue-file-name" data-file="${name}" title="${name}">${fileIcon('document')}<span>${name}</span></button>${entry.size == null ? '' : `<span class="file-size">${sizeLabel(entry.size)}</span>`}${preview}</div>`;
    }).join(''));
    fileStatus(folderEntries.length ? '' : '此目录为空');
    if (next == null) return;
    const more = document.createElement('button');
    more.type = 'button';
    more.textContent = '加载更多文件';
    $('job-files').append(more);
    more.onclick = async () => {
      more.disabled = true;
      try {
        const result = await api(fileURL(job,'directory',path,{offset:next}));
        if (request !== reading) return;
        more.remove();
        appendFiles(result.entries,result.next_offset,job,path,request);
      } catch(error) {
        if (request === reading) {fileStatus(error.message);more.disabled=false;}
      }
    };
  }

  function viewerStatus(text) {
    $('job-viewer-status').textContent = text;
    $('job-viewer-status').hidden = !text;
  }
  const viewer = createStructureBridge({
    frame, selectTab:() => {
      $('job-viewer').hidden = false;
      if (!frame.hasAttribute('src')) frame.src = '/structures?embed=1';
    }, toast:viewerStatus, onFrame:() => viewerStatus(''),
  });

  function closeFile() {
    textRequest?.abort();
    $('job-file-preview').hidden=true;
    $('job-file-body').replaceChildren();
    $('job-file-actions').replaceChildren();
  }

  function closeViewer() {
    closeFile();
    resourceRequest?.abort();
    if ($('job-viewer').hidden) return;
    $('job-viewer').hidden = true;
    activePreview = '';
    viewer.reset();
    viewerStatus('');
  }

  function closeJob() {
    if (!selected) return;
    const previous = key(selected);
    reading++;
    selected = null;
    detail.hidden = true;
    closeViewer();
    drawQueue(false);
    ([...jobList.querySelectorAll('[data-job]')].find(button => button.dataset.job === previous)
      || hostSelect).focus();
  }

  jobList.addEventListener('click', event => {
    const button = event.target.closest('[data-job]');
    if (!button) return;
    const job = results.flatMap(row => row.jobs || []).find(job => key(job) === button.dataset.job);
    if (!job) return;
    if (selected && key(selected) === key(job)) {closeJob();return;}
    selected = job;
    detail.hidden = false;
    renderJob();
    drawQueue(false);
    $('job-heading').focus();
    void loadFiles('');
  });
  async function openFile(name) {
    if (!selected) return;
    closeViewer();
    const path=[directory,name].filter(Boolean).join('/');
    const target=$('job-file-body'), actions=$('job-file-actions');
    textRequest = new AbortController();
    const {signal} = textRequest;
    $('job-file-name').textContent=name;
    $('job-file-preview').hidden=false;
    fileMessage(target,'正在读取文件…');
    try {
      const [previewUrl,contentUrl,mediaUrl]=['preview','content','media']
        .map(operation=>fileURL(selected,operation,path));
      await renderFilePreview({api,name,target,actions,signal,previewUrl,contentUrl,mediaUrl});
    } catch(error) {
      if (!signal.aborted) fileMessage(target,error.message,'alert');
    }
  }

  async function openPreview(name, companionNames, lastFrame = false) {
    if (!selected) return;
    closeFile();
    resourceRequest?.abort();
    resourceRequest = new AbortController();
    const {signal} = resourceRequest;
    const job = selected;
    const prefix = directory ? directory + '/' : '';
    const automatic = companionNames === undefined;
    $('job-viewer').hidden=false;
    $('job-viewer-name').textContent = lastFrame ? name + ' · 最新完整帧' : name;
    $('job-viewer-latest').hidden = !isDump(name);
    $('job-viewer-latest').textContent = lastFrame ? '完整轨迹' : '最新帧';
    $('job-viewer-latest').onclick = () => openPreview(name, undefined, !lastFrame);
    if (name !== activePreview || automatic) {
      activePreview = name;
      const options = folderEntries.filter(entry => !entry.directory && entry.name !== name
        && entry.size <= 32 * 1024 ** 2 && /\.(?:data|lmp|in|init|settings|lammps|log)$|^(?:log|in)\./i.test(entry.name));
      $('job-companions').open = false;
      $('job-companions').hidden = !/\.(?:lammpstrj|dump)$/i.test(name) || !options.length;
      companionNames = [];
      $('job-companion-options').innerHTML = options.map(entry =>
        `<label><input type="checkbox" value="${esc(entry.name)}" ${companionNames.includes(entry.name) ? "checked" : ""}><span>${esc(entry.name)}</span></label>`).join('');
    }
    viewerStatus('正在读取结构…');
    try {
      const size = folderEntries.filter(entry => companionNames.includes(entry.name))
        .reduce((total, entry) => total + (entry.size || 0), 0);
      if (size > 32 * 1024 ** 2 || companionNames.length > 31) {
        throw Error('配套文件最多 31 个，合计不超过 32 MiB。');
      }
      const path=prefix+name;
      const companions=automatic && isDump(name)
        ? api(jobURL(job,'metadata',path),{signal}).then(data=>data.companions).catch(()=>[])
        : Promise.all(companionNames.map(async filename=>{
          const blob=await getFileBlob(fileURL(job,'content',prefix+filename),{signal});
          return {name:filename,text:await blob.text()};
        }));
      const trajectory=isStreamableStructure(name) && !lastFrame
        ? {name,url:fileURL(job,'trajectory',path)} : null;
      const href=lastFrame ? jobURL(job,'latest-frame',path) : fileURL(job,'content',path);
      const files=await companions;
      if (!signal.aborted) await viewer.open(name,()=>getFileBlob(href,{signal}),{
        trajectory, companions:files, signal,
      });
    } catch(error) {
      if (!signal.aborted) viewerStatus(error.message);
    }
  }

  $('job-files').addEventListener('click', event => {
    const folder = event.target.closest('[data-folder]');
    if (folder) {void loadFiles([directory,folder.dataset.folder].filter(Boolean).join('/'));return;}
    const preview = event.target.closest('[data-preview]');
    if (preview) {void openPreview(preview.dataset.preview);return;}
    const file=event.target.closest('[data-file]');
    if (file) void openFile(file.dataset.file);
  });
  $('job-preview-with-companions').onclick = () => {
    const names = [...$('job-companion-options').querySelectorAll('input:checked')].map(input => input.value);
    $('job-companions').open = false;
    openPreview(activePreview,names);
  };
  $('job-parent').onclick = () => loadFiles(directory.split('/').slice(0,-1).join('/'));
  $('job-refresh').onclick = () => loadFiles(directory,false);
  $('job-close').onclick = closeJob;
  $('job-viewer-close').onclick = closeViewer;
  $('job-file-close').onclick = closeFile;
  $('queue-refresh').onclick = refresh;
  hostSelect.onchange = () => {if (selected && hostSelect.value && hostSelect.value !== selected.host) closeJob();drawQueue();};
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && visible && selected && !event.defaultPrevented && !document.querySelector('dialog[open]')) closeJob();
  });
  function switchView(showHistory) {
    if (showHistory === history) return;
    closeJob();
    history = showHistory;
    $('queue-active').setAttribute('aria-pressed', !history);
    $('queue-history').setAttribute('aria-pressed', history);
    $(history ? 'queue-history' : 'queue-active').focus();
    refreshing = false;
    results = [];
    jobList.replaceChildren();
    delete jobList.dataset.markup;
    $('queue-errors').replaceChildren();
    $('queue-updated').textContent = '';
    $('queue-count').textContent = history ? '正在读取历史作业…' : '正在读取队列…';
    void refresh();
  }
  $('queue-active').onclick = () => switchView(false);
  $('queue-history').onclick = () => switchView(true);
  setInterval(() => {if (visible && !document.hidden && !history) void refresh();},60000);
  return {
    show(open) {
      visible = open;
      if (open && (!loaded || !history)) void refresh();
      viewer.setVisible(open);
    },
  };
}
