export const effortLabel = value => ({'':'默认', none:'无', minimal:'极低', low:'低', medium:'中', high:'高', xhigh:'极高', extra:'极高', max:'最高', ultra:'Ultra', enabled:'思考', disabled:'关闭思考'}[value] || value);
export const permissionLabel = value => ({user:'批准', auto_review:'帮我批准', full:'完全访问'}[value] || value);

export function createCascadeMenu(anchor) {
  const menu = document.createElement('div');
  menu.className = 'agent-menu'; menu.hidden = true;
  if (menu.showPopover) menu.setAttribute('popover', 'manual');
  document.body.append(menu);
  let columns = [], compact = false;
  const compactLayout = () => innerWidth <= 640 || innerWidth < columns.length * 210 + 24;
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
      column.rows.forEach((row, index) => {
        if (row.message) { const note = document.createElement('div'); note.className = 'menu-message'; note.textContent = row.message; pane.append(note); return; }
        const button = document.createElement('button'); button.type = 'button'; button.setAttribute('role', row.branch ? 'menuitem' : 'menuitemradio');
        button.dataset.row = String(index);
        if (row.branch) {
          button.setAttribute('aria-haspopup', 'menu');
          button.setAttribute('aria-expanded', String(Boolean(columns[depth + 1] && column.activeRow === index)));
        }
        else button.setAttribute('aria-checked', String(Boolean(row.selected)));
        button.disabled = Boolean(row.disabled); button.title = row.title || '';
        button.dataset.active = String(Boolean(row.active || (columns[depth + 1] && column.activeRow === index)));
        const label = document.createElement('span'); label.textContent = row.label;
        if (row.noTranslate) label.dataset.noI18n = '';
        const trailing = document.createElement('span'); trailing.className = 'menu-trailing'; trailing.innerHTML = row.branch ? '<svg class="icon" aria-hidden="true"><use href="/assets/ui-icons.svg#i-chevron"/></svg>' : row.selected ? '<svg class="icon" aria-hidden="true"><use href="/assets/ui-icons.svg#i-check"/></svg>' : '';
        button.append(label, trailing); button.onclick = () => { column.activeRow = index; row.action?.(); }; pane.append(button);
      });
      if (column.extra) pane.append(column.extra());
      menu.append(pane);
    });
    place();
    if (focus) {
      const pane = menu.lastElementChild;
      const preferred = restoreRow !== null ? pane?.querySelector(`[data-row="${restoreRow}"]:not(:disabled)`) :
        pane?.querySelector('[aria-checked=true]:not(:disabled)') || pane?.querySelector('[data-active=true]:not(:disabled)');
      (preferred || pane?.querySelector('button[data-row]:not(:disabled)') || pane?.querySelector('button:not(:disabled)') || pane)?.focus();
    }
  }
  function goBack() {
    if (columns.length < 2) return;
    columns.pop(); draw(true, columns.at(-1).activeRow ?? null);
  }
  function column(depth, title, rows, extra) { columns.splice(depth, columns.length, {title, rows, extra}); draw(true); }
  function open(title, rows) {
    document.dispatchEvent(new CustomEvent('chem:menu-open', {detail:anchor}));
    menu.hidden = false; if (menu.showPopover) menu.showPopover();
    anchor.setAttribute('aria-expanded', 'true'); columns = [{title, rows}]; draw(true);
  }
  menu.addEventListener('keydown', event => {
    if (event.key === 'Escape') { event.preventDefault(); close(true); return; }
    const pane = event.target.closest('.agent-menu-column'); if (!pane || event.target.matches('input')) return;
    const buttons = [...pane.querySelectorAll('button:not(:disabled)')], index = buttons.indexOf(document.activeElement);
    if (['ArrowDown','ArrowUp','Home','End'].includes(event.key)) {
      event.preventDefault(); const next = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length-1 : (index + (event.key==='ArrowDown'?1:-1) + buttons.length) % buttons.length; buttons[next]?.focus();
    } else if (event.key === 'ArrowRight') { event.preventDefault(); if (event.target.hasAttribute('aria-haspopup')) event.target.click(); }
    else if (event.key === 'ArrowLeft' && columns.length > 1) { event.preventDefault(); goBack(); }
    else if (event.key === 'Tab') close(true);
  });
  document.addEventListener('pointerdown', event => { if (!menu.contains(event.target) && !anchor.contains(event.target)) close(); });
  document.addEventListener('chem:menu-open', event => { if (event.detail !== anchor) close(); });
  document.addEventListener('focusin', event => { if (!menu.hidden && !menu.contains(event.target) && !anchor.contains(event.target)) close(); });
  window.addEventListener('resize', () => {
    if (menu.hidden) return;
    if (compact !== compactLayout()) draw(menu.contains(document.activeElement)); else place();
  });
  window.addEventListener('scroll', place, true);
  return {open, close, column, isColumnOpen:(depth, title) => !menu.hidden && columns[depth]?.title === title, get visible() { return !menu.hidden; }};
}
