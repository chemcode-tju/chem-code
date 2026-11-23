import {escapeHtml as esc} from './core.mjs';
import {getLanguage, t} from './i18n.mjs';

const $ = id => document.getElementById(id);
const count = value => value.toLocaleString(getLanguage());
const sum = (rows, field) => rows.reduce((total, row) => total + row[field], 0);
const clock = hour => `${String(hour).padStart(2, '0')}:00`;
const resourceHours = seconds => seconds > 0 && seconds < 36 ? '<0.01' :
  (seconds / 3600).toLocaleString(getLanguage(), {maximumFractionDigits:2});
const usageUnits = {cpu:'CPU 核时', gpu:'GPU 小时'};
const heatLevels = [0, 34, 52, 70, 88];
const message = text => `<p class="job-board-note">${esc(t(text))}</p>`;

function mark(index, selected, label, style, body = '', unavailable = false) {
  const active = index === selected;
  return `<button type="button" data-index="${index}" aria-pressed="${active}" tabindex="${active ? 0 : -1}"
    aria-label="${esc(label)}" title="${esc(label)}" style="${style}"${unavailable ? ' data-history-missing' : ''}>${body}</button>`;
}

function chart(name, label, marks, selectedLabel, axis = '', legend = '') {
  return `<div data-board-chart="${name}">
    <div class="job-board-plot" role="group" aria-label="${esc(t(label))}">${marks}</div>${axis}
    <div class="job-board-chart-footer">
      <p class="job-board-reading" data-board-reading aria-live="polite">${esc(selectedLabel)}</p>${legend}
    </div>
  </div>`;
}

function calendar(days) {
  const offset = (new Date(`${days[0].date}T00:00:00Z`).getUTCDay() + 6) % 7;
  const columns = Math.ceil((days.length + offset) / 7);
  const activeDays = days.map(day => day.submitted).filter(value => value > 0).sort((a, b) => a - b);
  const quartiles = [0.25, 0.5, 0.75].map(part => activeDays[Math.ceil(part * activeDays.length) - 1]);
  const selected = days.length - 1;
  const label = day => t(day.submitted === null ? `${day.date} · 历史不可查` :
    `${day.date} · 提交 ${count(day.submitted)} 个作业`);
  const months = new Map();
  days.forEach(({date}, index) => {
    if (index === 0 || date.endsWith('-01')) {
      months.set(Math.floor((index + offset) / 7), Number(date.slice(5, 7)));
    }
  });
  const monthLabels = [...months].map(([week, month]) =>
    `<span class="job-board-month${week === columns - 1 ? ' is-last' : ''}" style="grid-column:${week + 2}" aria-hidden="true">${t(`${month}月`)}</span>`
  ).join('');
  const weekdayLabels = ['周一', '周三', '周五', '周日'].map((day, index) =>
    `<span class="job-board-weekday" style="grid-row:${2 * index + 2}">${t(day)}</span>`
  ).join('');
  const cells = days.map((day, index) => {
    const position = index + offset;
    const level = day.submitted ? 1 + quartiles.filter(limit => day.submitted > limit).length : 0;
    const color = heatLevels[level];
    const style = `grid-column:${Math.floor(position / 7) + 2};grid-row:${position % 7 + 2};--strength:${color}%`;
    return mark(index, selected, label(day), style, '', day.submitted === null);
  }).join('');
  const unavailable = days.filter(day => day.submitted === null).length;
  const legend = `<div class="job-board-scale" title="按有提交日期的作业数四分位分色" aria-hidden="true">
    ${unavailable ? '<i class="is-unavailable"></i><span>不可查</span>' : ''}<span>少</span>
    ${heatLevels.map(strength => `<i style="--strength:${strength}%"></i>`).join('')}
    <span>多</span></div>`;
  const summary = unavailable === days.length ? '所选范围暂无可查历史' :
    `提交 ${count(sum(days, 'submitted'))} 个作业`;
  return `<div class="job-board-calendar-head"><strong>${t(summary)}</strong>
    ${unavailable ? `<span>${t(`${count(unavailable)} 天历史不可查`)}</span>` : ''}</div>
    <div style="--weeks:${columns}">
      ${chart('calendar', '按日提交量', monthLabels + weekdayLabels + cells, label(days[selected]), '', legend)}
    </div>`;
}

function columns(name, label, values, labels, selected, ticks, missing = []) {
  const maximum = Math.max(1, ...values);
  const bars = values.map((value, index) => {
    const size = 100 * value / maximum;
    return mark(index, selected, labels[index], `--bar-size:${size}%`,
      `<span class="job-board-bar${missing[index] ? ' is-partial' : ''}" aria-hidden="true"></span>`);
  }).join('');
  const axis = `<div class="job-board-axis" aria-hidden="true">${ticks.map(tick => `<span>${tick}</span>`).join('')}</div>`;
  return chart(name, label, bars, labels[selected], axis);
}

function hourly(hours) {
  const peak = Math.max(...hours);
  const selected = hours.indexOf(peak);
  const labels = hours.map((value, hour) =>
    t(`${clock(hour)}–${clock(hour + 1)} · 提交 ${count(value)} 个作业`));
  const ties = hours.filter(value => value === peak).length;
  const peakLabel = `${clock(selected)}–${clock(selected + 1)}`;
  const summary = peak ? t(ties > 1 ? `高峰 ${peakLabel} 等 ${ties} 个时段` : `高峰 ${peakLabel}`) : t('暂无提交');
  return `<p class="job-board-summary"><strong>${esc(summary)}</strong></p>
    ${columns('hours', '按小时提交量', hours, labels, selected, ['00:00', '06:00', '12:00', '18:00', '24:00'])}`;
}

function gpuBreakdown(rows) {
  if (!rows.length) return '';
  const sources = {accounting:'Slurm 记录', node:'节点推断'};
  return `<div class="job-board-gpu-detail">
    <h3>按卡型与卡数</h3>
    <div class="job-board-gpu-table">
      <table aria-label="所选期间 GPU 分配明细">
        <thead><tr><th scope="col">卡型</th><th scope="col">卡数/作业</th>
          <th scope="col">时长 (h)</th><th scope="col">GPU 小时</th></tr></thead>
        <tbody>${rows.map(row => `<tr><th scope="row">
          ${row.model ? `<span data-no-i18n>${esc(row.model.toUpperCase())}</span>` : '型号未记录'}
          ${sources[row.source] ? `<small>${sources[row.source]}</small>` : ''}
        </th><td>${count(row.cards)}</td><td>${esc(resourceHours(row.elapsed_seconds))}</td>
          <td>${esc(resourceHours(row.gpu_seconds))}</td></tr>`).join('')}</tbody>
      </table>
    </div>
    ${rows.some(row => row.source === 'node') ? message('推断型号来自当前节点配置。') : ''}
  </div>`;
}

function usageBins(days, intervalDays) {
  if (intervalDays <= 31) return days.map(day => ({...day, end:day.date}));
  const groups = new Map();
  for (const day of days) {
    const date = new Date(`${day.date}T00:00:00Z`);
    date.setUTCDate(intervalDays > 366 ? 1 : date.getUTCDate() - (date.getUTCDay() + 6) % 7);
    const key = date.toISOString().slice(0, 10);
    const bin = groups.get(key);
    if (!bin) groups.set(key, {...day, end:day.date});
    else {
      bin.end = day.date;
      for (const field of ['cpu_seconds', 'gpu_seconds', 'missing_cpu', 'missing_gpu']) {
        bin[field] += day[field];
      }
    }
  }
  return [...groups.values()];
}

function usage(days, gpuAvailable, resource, selected, gpuRows, intervalDays) {
  const label = (seconds, unknown) => unknown && !seconds ? '—' : resourceHours(seconds);
  const bins = usageBins(days, intervalDays);
  const selectedBin = selected ?? bins.length - 1;
  const buttons = Object.entries(usageUnits).map(([key, unit]) => {
    const available = key === 'cpu' || gpuAvailable;
    const total = available ? label(sum(days, `${key}_seconds`), sum(days, `missing_${key}`)) : '—';
    return `<button type="button" data-usage-resource="${key}" aria-pressed="${key === resource}"
      ${available ? '' : 'disabled title="Slurm GPU 机时记录不可用。"'}><strong>${esc(total)}</strong><span>${unit}</span></button>`;
  }).join('');
  const labels = bins.map(day => {
    const cpu = label(day.cpu_seconds, day.missing_cpu);
    const gpu = gpuAvailable ? label(day.gpu_seconds, day.missing_gpu) : '—';
    const range = day.date === day.end ? day.date : `${day.date} – ${day.end}`;
    return t(`${range} · ${cpu} CPU 核时 · ${gpu} GPU 小时`);
  });
  const stride = Math.ceil(bins.length / 6);
  const ticks = bins.map((day, index) => index % stride === 0 ? day.date.slice(5) : '');
  const missing = bins.map(day => day[`missing_${resource}`]);
  const missingCount = sum(days, `missing_${resource}`);
  const chartLabel = intervalDays > 366 ? '按月机时' : intervalDays > 31 ? '按周机时' : `按日 ${usageUnits[resource]}`;
  return `<div class="job-board-usage-metrics" role="group" aria-label="机时图表指标">${buttons}</div>
    ${columns('usage', chartLabel, bins.map(day => day[`${resource}_seconds`]), labels, selectedBin, ticks, missing)}
    ${missingCount ? message(`${count(missingCount)} 个作业缺少 ${usageUnits[resource]}记录`) : ''}
    ${resource === 'gpu' ? gpuBreakdown(gpuRows) : ''}
    ${message(resource === 'gpu' ? 'GPU 小时表示分配卡数 × 作业时长，按结束日归集。' :
      'CPU 核时按分配 CPU 数 × 作业时长计算，归至结束日。')}`;
}

function liveStatus(active) {
  const metrics = [['运行中', 'running'], ['排队中', 'queued'], ['其他状态', 'other']];
  return `<h3>当前队列</h3><dl class="job-board-live-metrics">${metrics
    .filter(([, key]) => key !== 'other' || active?.other)
    .map(([label, key]) => `<div data-state="${key}"><dt>${label}</dt><dd>${active ? count(active[key]) : '—'}</dd></div>`)
    .join('')}</dl>`;
}

function terminalStatus(days) {
  const totals = [['已完成', 'completed'], ['失败', 'failed'], ['已取消', 'cancelled']]
    .map(([label, key]) => ({label, key, value:days ? sum(days, key) : null}));
  const max = Math.max(1, ...totals.map(row => row.value ?? 0));
  return `<dl class="job-board-status-list">${totals.map(({label, key, value}) => `<div data-state="${key}">
    <dt>${label}</dt><dd><span aria-hidden="true" class="job-board-status-track">
    <span style="width:${100 * (value ?? 0) / max}%"></span></span><span>${value === null ? '—' : count(value)}</span></dd>
  </div>`).join('')}</dl>`;
}

export function initJobBoard() {
  const host = $('job-board-host');
  const grid = $('job-board-grid');
  const status = $('job-board-message');
  const refresh = $('job-board-refresh');
  const range = $('job-board-range');
  const coverage = $('job-board-coverage');
  const calendarPanel = $('job-board-calendar');
  const usagePanel = $('job-board-usage-data');
  let resource = 'cpu';
  let data = null;
  let request = null;

  function keepLatestDayInView() {
    const plot = calendarPanel.querySelector('.job-board-plot');
    if (plot?.querySelector('button:last-of-type[aria-pressed="true"]')) {
      plot.scrollLeft = plot.scrollWidth;
    }
  }
  new ResizeObserver(keepLatestDayInView).observe(calendarPanel);

  async function get(url, signal) {
    const response = await fetch(url, {cache:'no-store', signal});
    if (!response.ok) throw new Error('看板数据读取失败。');
    return response.json();
  }

  function renderUsage(selected) {
    if (!data.gpu_accounting_available) resource = 'cpu';
    const availableDays = data.days.filter(day => day.submitted !== null);
    usagePanel.innerHTML = availableDays.length ?
      usage(availableDays, data.gpu_accounting_available, resource, selected,
        data.gpu_breakdown ?? [], data.days.length) :
      message('Slurm 机时记录不可用。');
  }

  function render() {
    const {days, active, accounting_available: accounting, observed_at: observed, submission_hours: hours} = data;
    const availableDays = days.filter(day => day.submitted !== null);
    calendarPanel.innerHTML = accounting ? calendar(days) : message('历史提交记录不可用。');
    $('job-board-hours').innerHTML = availableDays.length ? hourly(hours) : message('历史提交时段不可用。');
    $('job-board-live').innerHTML = liveStatus(active);
    renderUsage();
    $('job-board-terminal').innerHTML = terminalStatus(availableDays.length ? availableDays : null);
    const time = $('job-board-observed');
    time.dateTime = observed;
    time.textContent = observed.slice(0, 16).replace('T', ' ');
    coverage.hidden = !accounting;
    const dates = days.length === 1 ? days[0].date : `${days[0].date} – ${days.at(-1).date}`;
    coverage.textContent = availableDays.length && availableDays.length < days.length ?
      t(`${dates} · ${availableDays.length} 天可查`) : dates;
    $('job-board-freshness').hidden = false;
    grid.hidden = false;
    keepLatestDayInView();
    const archived = data.history_synced_at && !data.live_accounting_available;
    status.textContent = t(archived ? 'Slurm 历史暂不可读，显示平台留存。' :
      !accounting && active === null ? '当前队列和 Slurm 历史均不可用。' :
      !accounting ? 'Slurm 历史不可用，当前队列仍可查看。' :
      active === null ? '当前队列不可用。' :
      data.history_synced_at ? `平台已留存 · ${data.history_synced_at.slice(0, 16).replace('T', ' ')}` : '');
  }

  async function load(reloadHosts, clear = false) {
    request?.abort();
    const controller = new AbortController();
    request = controller;
    const {signal} = controller;
    if (clear || !data) {
      data = null;
      grid.hidden = true;
      $('job-board-freshness').hidden = true;
      coverage.hidden = true;
    }
    status.textContent = t('正在读取看板…');
    refresh.setAttribute('aria-busy', 'true');
    try {
      if (reloadHosts) {
        const {hosts} = await get('/api/job-monitor/hosts', signal);
        signal.throwIfAborted();
        const previous = host.value;
        host.replaceChildren(...hosts.map(({id, name}) => {
          const option = new Option(name, id);
          option.dataset.noI18n = '';
          return option;
        }));
        host.disabled = !hosts.length;
        range.disabled = host.disabled;
        if (hosts.some(item => item.id === previous)) host.value = previous;
        if (host.value !== previous) {
          data = null;
          grid.hidden = true;
          $('job-board-freshness').hidden = true;
          coverage.hidden = true;
        }
      }
      if (!host.value) {
        status.textContent = t('没有可用的 Slurm 服务器。');
        return;
      }
      const result = await get(`/api/job-monitor/${encodeURIComponent(host.value)}/dashboard?period=${range.value}`, signal);
      signal.throwIfAborted();
      data = result;
      render();
    } catch {
      if (!signal.aborted) status.textContent = t(data ? '看板刷新失败，显示上次结果。' : '看板数据读取失败。');
    } finally {
      if (request === controller) {
        request = null;
        refresh.removeAttribute('aria-busy');
      }
    }
  }

  function selectMark(button) {
    const chartRoot = button.closest('[data-board-chart]');
    const selected = chartRoot.querySelector('[aria-pressed="true"]');
    selected.tabIndex = -1;
    selected.setAttribute('aria-pressed', 'false');
    button.tabIndex = 0;
    button.setAttribute('aria-pressed', 'true');
    chartRoot.querySelector('[data-board-reading]').textContent = button.getAttribute('aria-label');
  }

  grid.addEventListener('click', event => {
    const button = event.target.closest('button[data-index]');
    if (button) selectMark(button);
  });
  usagePanel.addEventListener('click', event => {
    const button = event.target.closest('button[data-usage-resource]');
    if (!button || button.disabled || button.dataset.usageResource === resource) return;
    resource = button.dataset.usageResource;
    const selected = usagePanel.querySelector('button[data-index][aria-pressed="true"]');
    renderUsage(Number(selected.dataset.index));
    usagePanel.querySelector(`button[data-usage-resource="${resource}"]`).focus({preventScroll:true});
  });
  grid.addEventListener('keydown', event => {
    const button = event.target.closest('button[data-index]');
    if (!button) return;
    const chartRoot = button.closest('[data-board-chart]');
    const marks = [...chartRoot.querySelectorAll('button[data-index]')];
    const step = chartRoot.dataset.boardChart === 'calendar' ? 7 : 1;
    const moves = {ArrowRight:step, ArrowLeft:-step, ArrowDown:1, ArrowUp:-1};
    const index = Number(button.dataset.index);
    const next = event.key === 'Home' ? marks[0] : event.key === 'End' ? marks.at(-1) :
      marks[index + moves[event.key]];
    if (!next) return;
    event.preventDefault();
    selectMark(next);
    next.focus();
  });
  range.onchange = () => void load(false, true);
  host.onchange = () => void load(false, true);
  refresh.onclick = () => void load(true);
  return {
    show(visible) {
      if (visible) void load(true);
      else request?.abort();
    },
  };
}
