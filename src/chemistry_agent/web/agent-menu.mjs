export const effortLabel = value => ({'':'默认', none:'无', minimal:'极低', low:'低', medium:'中', high:'高', xhigh:'极高', extra:'极高', max:'最高', ultra:'Ultra', enabled:'思考', disabled:'关闭思考'}[value] || value);
export const permissionLabel = value => ({user:'批准', auto_review:'帮我批准', full:'Full access'}[value] || value);

export function createCascadeMenu(anchor) {
  const menu = document.createElement('div');
  menu.className = 'agent-menu'; menu.hidden = true; menu.dataset.noI18n = '';
  if (menu.showPopover) menu.setAttribute('popover', 'manual');
  document.body.append(menu);
  let columns = [];
  function place() {
    if (menu.hidden) return;
    const rect = anchor.getBoundingClientRect();
    menu.style.left = `${Math.max(8, Math.min(rect.right - menu.offsetWidth, innerWidth - menu.offsetWidth - 8))}px`;
    menu.style.top = `${Math.max(8, rect.top - menu.offsetHeight - 8)}px`;
  }
  function close(focus = false) {
    if (menu.hidden) return;
    if (menu.hidePopover) menu.hidePopover();
    menu.hidden = true; anchor.setAttribute('aria-expanded', 'false');
    if (focus) anchor.focus();
  }
  function draw(focus = false) {
    menu.replaceChildren();
    columns.forEach((column, depth) => {
      const pane = document.createElement('div'); pane.className = 'agent-menu-column'; pane.setAttribute('role', 'menu'); pane.setAttribute('aria-label', column.title);
      if (depth && matchMedia('(max-width:640px)').matches) {
        const back = document.createElement('button'); back.type = 'button'; back.textContent = '‹ 返回';
        back.onclick = () => { columns.splice(depth); draw(true); }; pane.append(back);
      }
      const title = document.createElement('div'); title.className = 'agent-menu-title'; title.textContent = column.title; pane.append(title);
      column.rows.forEach(row => {
        if (row.message) { const note = document.createElement('div'); note.className = 'menu-message'; note.textContent = row.message; pane.append(note); return; }
        const button = document.createElement('button'); button.type = 'button'; button.setAttribute('role', row.branch ? 'menuitem' : 'menuitemradio');
        if (row.branch) button.setAttribute('aria-haspopup', 'menu');
        else button.setAttribute('aria-checked', String(Boolean(row.selected)));
        button.disabled = Boolean(row.disabled); button.title = row.title || '';
        button.dataset.active = String(Boolean(row.active));
        const label = document.createElement('span'); label.textContent = row.label;
        const trailing = document.createElement('span'); trailing.className = 'menu-trailing'; trailing.textContent = row.branch ? '›' : row.selected ? '✓' : '';
        button.append(label, trailing); button.onclick = () => row.action?.(); pane.append(button);
      });
      if (column.extra) pane.append(column.extra());
      menu.append(pane);
    });
    place();
    if (focus) menu.lastElementChild?.querySelector('button:not(:disabled)')?.focus();
  }
  function column(depth, title, rows, extra) { columns.splice(depth, columns.length, {title, rows, extra}); draw(true); }
  function open(title, rows) {
    document.dispatchEvent(new CustomEvent('chem:agent-menu-open', {detail:anchor}));
    menu.hidden = false; if (menu.showPopover) menu.showPopover();
    anchor.setAttribute('aria-expanded', 'true'); columns = [{title, rows}]; draw(true);
  }
  menu.addEventListener('keydown', event => {
    const pane = event.target.closest('.agent-menu-column'); if (!pane || event.target.matches('input')) return;
    const buttons = [...pane.querySelectorAll('button:not(:disabled)')], index = buttons.indexOf(document.activeElement);
    if (['ArrowDown','ArrowUp','Home','End'].includes(event.key)) {
      event.preventDefault(); const next = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length-1 : (index + (event.key==='ArrowDown'?1:-1) + buttons.length) % buttons.length; buttons[next]?.focus();
    } else if (event.key === 'ArrowRight') { event.preventDefault(); if (event.target.hasAttribute('aria-haspopup')) event.target.click(); }
    else if (event.key === 'ArrowLeft' && columns.length > 1) { event.preventDefault(); columns.pop(); draw(true); }
    else if (event.key === 'Escape') { event.preventDefault(); close(true); }
    else if (event.key === 'Tab') close();
  });
  document.addEventListener('pointerdown', event => { if (!menu.contains(event.target) && !anchor.contains(event.target)) close(); });
  document.addEventListener('chem:agent-menu-open', event => { if (event.detail !== anchor) close(); });
  window.addEventListener('resize', () => draw());
  window.addEventListener('scroll', place, true);
  return {open, close, column, get visible() { return !menu.hidden; }};
}
