export const effortLabel = value => ({'':'默认', none:'无', minimal:'极低', low:'低', medium:'中', high:'高', xhigh:'极高', extra:'极高', max:'最高', ultra:'Ultra', enabled:'思考', disabled:'关闭思考'}[value] || value);
export const permissionLabel = value => ({user:'请求批准', auto_review:'帮我批准', full:'完全访问权限'}[value] || value);

export function createAnchoredPanel(anchor, menu) {
  menu.hidden = true;
  if (menu.showPopover) menu.setAttribute('popover', 'manual');
  document.body.append(menu);
  function place() {
    if (menu.hidden) return;
    const rect = anchor.getBoundingClientRect();
    if (!anchor.isConnected || !anchor.getClientRects().length || rect.bottom < 0 || rect.top > innerHeight) { close(); return; }
    const above = rect.top - 16, below = innerHeight - rect.bottom - 16;
    menu.style.maxHeight = `${Math.max(44, Math.min(420, Math.max(above, below)))}px`;
    menu.style.left = `${Math.max(8, Math.min(rect.right - menu.offsetWidth, innerWidth - menu.offsetWidth - 8))}px`;
    const up = above >= menu.offsetHeight || above > below;
    menu.style.top = `${Math.max(8, up ? rect.top - menu.offsetHeight - 8 : rect.bottom + 8)}px`;
  }
  function close(focus = false) {
    if (menu.hidden) return;
    if (menu.hidePopover) menu.hidePopover();
    menu.hidden = true; anchor.setAttribute('aria-expanded', 'false');
    if (focus) anchor.focus();
  }
  function open() {
    document.dispatchEvent(new CustomEvent('chem:menu-open', {detail:anchor}));
    menu.hidden = false;
    if (menu.showPopover) menu.showPopover();
    anchor.setAttribute('aria-expanded', 'true');
    place();
  }

  menu.addEventListener('keydown', event => {
    if (event.key === 'Escape' && !event.defaultPrevented) { event.preventDefault(); close(true); }
  });
  document.addEventListener('pointerdown', event => { if (!menu.contains(event.target) && !anchor.contains(event.target)) close(); });
  document.addEventListener('chem:menu-open', event => { if (event.detail !== anchor) close(); });
  document.addEventListener('focusin', event => { if (!menu.hidden && !menu.contains(event.target) && !anchor.contains(event.target)) close(); });
  window.addEventListener('resize', place);
  window.addEventListener('scroll', place, true);
  new ResizeObserver(place).observe(menu);
  return {open, close, reposition:place, get visible() { return !menu.hidden; }};
}

export function createCascadeMenu(anchor, className = '') {
  const menu = document.createElement('div');
  menu.className = ['agent-menu', className].filter(Boolean).join(' ');
  const popup = createAnchoredPanel(anchor, menu);
  let columns = [], compact = false;
  const compactLayout = () => innerWidth <= 640 || innerWidth < columns.length * 210 + 24;
  function draw(focus = false, restoreRow = null) {
    compact = compactLayout();
    menu.dataset.compact = String(compact);
    menu.replaceChildren();
    columns.forEach((column, depth) => {
      const pane = document.createElement('div'); pane.className = 'agent-menu-column'; pane.tabIndex = -1; pane.setAttribute('role', 'menu'); pane.setAttribute('aria-label', column.title);
      if (depth && compact) {
        const back = document.createElement('button'); back.type = 'button'; back.innerHTML = '<svg class="icon" aria-hidden="true"><use href="/assets/ui-icons.svg#i-chevron-left"/></svg><span>返回</span>';
        back.setAttribute('role', 'menuitem');
        back.onclick = () => goBack(); pane.append(back);
      }
      const title = document.createElement('div'); title.className = 'agent-menu-title'; title.textContent = column.title; pane.append(title);
      let search;
      if (column.search) {
        search = document.createElement('input'); search.type = 'search'; search.className = 'menu-model-search';
        search.placeholder = column.search; search.setAttribute('aria-label', column.search);
        search.value = column.query || '';
        search.oninput = () => {
          column.query = search.value;
          for (const item of pane.querySelectorAll('[data-search]')) item.hidden = !item.dataset.search.includes(search.value.trim().toLocaleLowerCase());
        };
        search.onkeydown = event => {
          if (event.key === 'ArrowDown') {event.preventDefault();pane.querySelector('button[data-row]:not([hidden]):not(:disabled)')?.focus();}
          if (event.key === 'ArrowLeft' && !search.value) {event.preventDefault();goBack();}
        };
        pane.append(search);
      }
      column.rows.forEach((row, index) => {
        if (row.message) { const note = document.createElement('div'); note.className = 'menu-message'; note.textContent = row.message; pane.append(note); return; }
        const button = document.createElement('button'); button.type = 'button'; button.setAttribute('role', row.branch || row.command ? 'menuitem' : 'menuitemradio');
        button.dataset.row = String(index);
        if (row.branch) {
          button.setAttribute('aria-haspopup', 'menu');
          button.setAttribute('aria-expanded', String(Boolean(columns[depth + 1] && column.activeRow === index)));
          if (row.selected) button.setAttribute('aria-current', 'true');
        }
        else if (!row.command) button.setAttribute('aria-checked', String(Boolean(row.selected)));
        button.disabled = Boolean(row.disabled); button.title = row.title || '';
        // Selection is a check mark; only the currently open branch gets a fill.
        button.dataset.active = String(Boolean(columns[depth + 1] && column.activeRow === index));
        const label = document.createElement('span'); label.textContent = row.label;
        label.className = 'menu-label';
        if (row.noTranslate) label.dataset.noI18n = '';
        const trailing = document.createElement('span'); trailing.className = 'menu-trailing'; trailing.innerHTML = (row.selected ? '<svg class="icon" aria-hidden="true"><use href="/assets/ui-icons.svg#i-check"/></svg>' : '') + (row.branch ? '<svg class="icon" aria-hidden="true"><use href="/assets/ui-icons.svg#i-chevron"/></svg>' : '');
        if (row.icon) button.insertAdjacentHTML('beforeend', `<svg class="icon" aria-hidden="true"><use href="/assets/ui-icons.svg#i-${row.icon}"/></svg>`);
        if (row.warning) button.classList.add('menu-option-warning');
        button.append(label);
        if (row.value !== undefined) {
          button.classList.add('menu-setting-row');
          const value = document.createElement('span'); value.className = 'menu-current-value'; value.textContent = row.value;
          value.title = row.value;if(row.valueNoTranslate) value.dataset.noI18n = '';
          button.append(value);
        }
        button.append(trailing);
        if (row.searchText !== undefined) {
          button.dataset.search = row.searchText.toLocaleLowerCase();
          button.hidden = Boolean(column.query && !button.dataset.search.includes(column.query.trim().toLocaleLowerCase()));
        }
        button.onclick = () => { column.activeRow = index; row.action?.(); }; pane.append(button);
      });
      if (column.extra) pane.append(column.extra());
      menu.append(pane);
    });
    popup.reposition();
    if (focus) {
      const pane = menu.lastElementChild;
      const preferred = restoreRow !== null ? pane?.querySelector(`[data-row="${restoreRow}"]:not(:disabled)`) :
        pane?.querySelector('[aria-checked=true]:not(:disabled)') || pane?.querySelector('[aria-current=true]:not(:disabled)') || pane?.querySelector('[data-active=true]:not(:disabled)');
      (preferred || pane?.querySelector('button[data-row]:not(:disabled)') || pane?.querySelector('button:not(:disabled)') || pane)?.focus();
    }
  }
  function goBack() {
    if (columns.length < 2) return;
    columns.pop(); draw(true, columns.at(-1).activeRow ?? null);
  }
  function column(depth, title, rows, extra, search) { columns.splice(depth, columns.length, {title, rows, extra, search}); draw(true); }
  function open(title, rows) {
    columns = [{title, rows}];
    popup.open();
    draw(true);
  }
  menu.addEventListener('keydown', event => {
    const pane = event.target.closest('.agent-menu-column'); if (!pane || event.target.matches('input')) return;
    const buttons = [...pane.querySelectorAll('button:not(:disabled):not([hidden])')], index = buttons.indexOf(document.activeElement);
    if (['ArrowDown','ArrowUp','Home','End'].includes(event.key)) {
      event.preventDefault(); const next = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length-1 : (index + (event.key==='ArrowDown'?1:-1) + buttons.length) % buttons.length; buttons[next]?.focus();
    } else if (event.key === 'ArrowRight') { event.preventDefault(); if (event.target.hasAttribute('aria-haspopup')) event.target.click(); }
    else if (event.key === 'ArrowLeft' && columns.length > 1) { event.preventDefault(); goBack(); }
    else if (event.key === 'Tab') popup.close(true);
  });
  window.addEventListener('resize', () => {
    if (!popup.visible) return;
    if (compact !== compactLayout()) draw(menu.contains(document.activeElement));
  });
  return {open, close:popup.close, column, reposition:popup.reposition,
    isColumnOpen:(depth, title) => popup.visible && columns[depth]?.title === title,
    get visible() { return popup.visible; }};
}
