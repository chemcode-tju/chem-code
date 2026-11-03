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
    document.querySelector('[data-dialog="'+dialog.id+'"]').onclick=()=>dialog.showModal();
    dialog.querySelector('[data-close]').onclick=()=>dialog.close();
  }
  return setMaximized;
}
