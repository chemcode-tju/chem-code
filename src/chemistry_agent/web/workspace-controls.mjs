export function initWorkspaceControls() {
  const body=document.body, dock=document.getElementById('inspector'), grip=document.getElementById('dock-grip');
  const maximize=document.getElementById('dock-max');
  function resize(width) {
    width=Math.round(Math.max(300,Math.min(innerWidth-300,width)));
    body.style.setProperty('--dock-width',width+'px');
    grip.setAttribute('aria-valuenow',String(width));
    grip.setAttribute('aria-valuemax',String(Math.max(300,innerWidth-300)));
  }
  try { const value=Number(localStorage.getItem('chemistry.dockWidth')); if(value>0) resize(value); } catch {}
  function remember() {
    try {localStorage.setItem('chemistry.dockWidth',String(dock.offsetWidth));} catch {}
  }
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
    resize(event.key==='Home'?300:event.key==='End'?innerWidth-300:dock.offsetWidth+(event.key==='ArrowLeft'?32:-32));
    remember();
  };
  maximize.onclick=()=>{
    const active=body.classList.toggle('dock-max');
    maximize.setAttribute('aria-pressed',String(active));
    maximize.title=active?'还原面板':'最大化面板';
  };
  const screen=document.getElementById('step-screen');
  document.getElementById('step-full').onclick=()=>{
    if(document.fullscreenElement===screen) document.exitFullscreen();
    else screen.requestFullscreen().catch(()=>maximize.click());
  };
  for(const dialog of document.querySelectorAll('dialog[data-workspace-dialog]')) {
    document.querySelector('[data-dialog="'+dialog.id+'"]').onclick=()=>dialog.showModal();
    dialog.querySelector('[data-close]').onclick=()=>dialog.close();
  }
}
