export function initWorkspaceControls({closeInspector}) {
  const body=document.body, dock=document.getElementById('inspector'), grip=document.getElementById('dock-grip');
  const maximize=document.getElementById('dock-max');
  // Wrapped dock tabs and the chat title share one continuous header band.
  const dockHeader=dock.querySelector('.inspector-strip');
  new ResizeObserver(()=>{
    body.style.setProperty('--workspace-header-height',Math.max(44,dockHeader.offsetHeight)+'px');
  }).observe(dockHeader);
  const sidebarWidth = () => document.getElementById('sidebar').offsetWidth || parseFloat(getComputedStyle(body).getPropertyValue('--sidebar')) || 0;
  const maximumWidth = () => Math.max(300,innerWidth-sidebarWidth()-40);
  function resize(width) {
    width=Math.round(Math.max(300,Math.min(maximumWidth(),width)));
    body.style.setProperty('--dock-width',width+'px');
    grip.setAttribute('aria-valuenow',String(width));
  }
  function remember() {
    try {localStorage.setItem('chemistry.dockWidth',String(parseFloat(body.style.getPropertyValue('--dock-width')) || dock.offsetWidth));} catch {}
  }
  try {const saved=Number(localStorage.getItem('chemistry.dockWidth'));if(saved>0) resize(saved);} catch {}
  new ResizeObserver(()=>{
    grip.setAttribute('aria-valuemin','300');grip.setAttribute('aria-valuemax',String(innerWidth));
    grip.setAttribute('aria-valuenow',String(dock.offsetWidth));
  }).observe(dock);
  let drag=null;
  function preview(x) {
    const width=drag.width+drag.x-x;
    body.dataset.dockSnap=width<240?'close':x<=sidebarWidth()+24?'full':'resize';
    if(body.classList.contains('dock-max') && body.dataset.dockSnap!=='full') setMaximized(false);
    if(body.dataset.dockSnap==='resize') resize(width);
  }
  grip.onpointerdown=e=>{
    if(e.button!==0 || !e.isPrimary) return;
    e.preventDefault();grip.focus({preventScroll:true});grip.setPointerCapture(e.pointerId);
    drag={id:e.pointerId,x:e.clientX,width:dock.offsetWidth,full:body.classList.contains('dock-max')};
    body.classList.add('dock-drag');
  };
  grip.onpointermove=e=>{if(drag?.id===e.pointerId && Math.abs(e.clientX-drag.x)>3) preview(e.clientX);};
  function finish(e,cancel=false) {
    if(drag?.id!==e.pointerId) return;
    if(!cancel && Math.abs(e.clientX-drag.x)>3) preview(e.clientX);
    const snap=body.dataset.dockSnap, before=drag;drag=null;
    body.classList.remove('dock-drag');delete body.dataset.dockSnap;
    if(cancel || !snap) {setMaximized(before.full);if(!before.full) resize(before.width);return;}
    if(snap==='close' || snap==='full') {
      if(!before.full) resize(before.width);
      if(snap==='close') closeInspector();else setMaximized(true);
    } else remember();
  }
  grip.onpointerup=e=>finish(e);
  grip.onpointercancel=e=>finish(e,true);
  grip.onlostpointercapture=e=>{if(drag) finish(e,true);};
  grip.onkeydown=e=>{
    if(!['ArrowLeft','ArrowRight','Home','End'].includes(e.key)) return;
    e.preventDefault();
    if(e.key==='Home') {setMaximized(false);resize(300);remember();return;}
    const requestedWidth=parseFloat(body.style.getPropertyValue('--dock-width')) || dock.offsetWidth;
    if(e.key==='End' || (e.key==='ArrowLeft' && requestedWidth>=maximumWidth())) {setMaximized(true);return;}
    if(e.key==='ArrowRight' && requestedWidth<=300) {closeInspector();return;}
    const width=body.classList.contains('dock-max')?maximumWidth():requestedWidth;
    setMaximized(false);resize(width+(e.key==='ArrowLeft'?32:-32));remember();
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
