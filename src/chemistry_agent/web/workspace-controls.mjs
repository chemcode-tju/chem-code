export function initWorkspaceControls() {
  const body=document.body, dock=document.getElementById('inspector'), grip=document.getElementById('dock-grip');
  const maximize=document.getElementById('dock-max');
  function maximumWidth() {
    const sidebar = parseFloat(getComputedStyle(body).getPropertyValue('--sidebar')) || 0;
    return Math.max(300, Math.floor(innerWidth - sidebar - 360));
  }
  function resize(width) {
    width=Math.round(Math.max(300,Math.min(maximumWidth(),width)));
    body.style.setProperty('--dock-width',width+'px');
    grip.setAttribute('aria-valuenow',String(width));
    grip.setAttribute('aria-valuemax',String(maximumWidth()));
  }
  try { const value=Number(localStorage.getItem('chemistry.dockWidth')); if(value>0) resize(value); } catch {}
  function remember() {
    try {localStorage.setItem('chemistry.dockWidth',String(dock.offsetWidth));} catch {}
  }
  new ResizeObserver(() => {
    if (!body.classList.contains('inspector-open') || body.classList.contains('dock-max')) return;
    grip.setAttribute('aria-valuenow', String(dock.offsetWidth));
    grip.setAttribute('aria-valuemax', String(maximumWidth()));
  }).observe(dock);
  grip.onpointerdown=event=>{
    if(event.button) return;
    grip.setPointerCapture(event.pointerId);
    body.classList.add('dock-drag');
  };
  grip.onpointermove=event=>{if(grip.hasPointerCapture(event.pointerId)) resize(innerWidth-event.clientX);};
  grip.onlostpointercapture=()=>{body.classList.remove('dock-drag');remember();};
  grip.onkeydown=event=>{
    if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) return;
    event.preventDefault();
    resize(event.key==='Home'?300:event.key==='End'?maximumWidth():dock.offsetWidth+(event.key==='ArrowLeft'?32:-32));
    remember();
  };
  function setMaximized(active) {
    body.classList.toggle('dock-max', active);
    maximize.setAttribute('aria-pressed', String(active));
    const label = active ? '还原面板' : '最大化面板';
    maximize.title = label;
    maximize.setAttribute('aria-label', label);
    maximize.querySelector('use').setAttribute('href', '/assets/ui-icons.svg#i-' + (active ? 'shrink' : 'expand'));
  }
  maximize.onclick = () => setMaximized(!body.classList.contains('dock-max'));
  for(const dialog of document.querySelectorAll('dialog[data-workspace-dialog]')) {
    const trigger = document.querySelector('[data-dialog="'+dialog.id+'"]');
    if (dialog.id === 'context-panel') {
      bindContextPanel(dialog, trigger);
      continue;
    }
    trigger.onclick=()=>dialog.showModal();
    dialog.querySelector('[data-close]').onclick=()=>dialog.close();
  }
  return setMaximized;
}

/** Keep the context details beside the composer while leaving the chat usable. */
function bindContextPanel(panel, trigger) {
  function place() {
    if (!panel.open) return;
    const anchor = trigger.getBoundingClientRect();
    if (!anchor.width) { panel.close(); return; }
    const composer = trigger.closest(".composer").getBoundingClientRect();
    const gap = 8, above = anchor.top - gap, below = innerHeight - anchor.bottom - gap;
    const height = panel.offsetHeight;
    const y = height <= above || above >= below ? anchor.top - gap - height : anchor.bottom + gap;
    panel.style.top = Math.max(gap, Math.min(y, innerHeight - gap - height)) + "px";
    panel.style.left = Math.max(gap, Math.min(composer.right - panel.offsetWidth, innerWidth - gap - panel.offsetWidth)) + "px";
  }
  function closeAndFocus() { panel.close(); trigger.focus({preventScroll:true}); }
  trigger.onclick = () => {
    if (panel.open) { closeAndFocus(); return; }
    panel.show(); place(); trigger.setAttribute("aria-expanded", "true");
  };
  panel.querySelector("[data-close]").onclick = closeAndFocus;
  panel.addEventListener("close", () => { if (!panel.open) trigger.setAttribute("aria-expanded", "false"); });
  document.addEventListener("pointerdown", event => {
    if (panel.open && !panel.contains(event.target) && !trigger.contains(event.target)) panel.close();
  }, true);
  document.addEventListener("keydown", event => {
    if (panel.open && event.key === "Escape" && !event.defaultPrevented) {
      event.preventDefault(); closeAndFocus();
    }
  }, true);
  new ResizeObserver(place).observe(panel);
  window.addEventListener("resize", place);
  window.addEventListener("scroll", event => { if (panel.open && !(event.target instanceof Node && panel.contains(event.target))) place(); }, true);
}
