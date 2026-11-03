// Capture only after the drag threshold: a stationary press still opens the step.
export function bindMapGestures({board,view,grip,resize,zoomBy,camera,panTo,getScale,save,startNode,select,move,endNode}) {
  let drag=null, ignoreClick=false, frame=0, space=false;
  const listeners=new AbortController();
  document.addEventListener('keydown',e=>{
    if(e.code!=='Space' || e.target.closest('input,select,textarea,[contenteditable=true]')) return;
    if(view.contains(e.target) || view.matches(':hover')) {
      e.preventDefault();space=true;view.classList.add('pan-ready');
    }
  },{signal:listeners.signal});
  const release=()=>{space=false;view.classList.remove('pan-ready');};
  board.addEventListener('mapdispose',()=>listeners.abort(),{once:true});
  document.addEventListener('keyup',e=>{if(e.code==='Space') release();},{signal:listeners.signal});
  window.addEventListener('blur',release,{signal:listeners.signal});
  board.addEventListener('focusout',e=>{if(!board.contains(e.relatedTarget)) release();});
  function busy(on) {
    if(on) board.dataset.gesture='';
    else {delete board.dataset.gesture;board.dispatchEvent(new Event('mapidle',{bubbles:true}));}
  }
  view.addEventListener('wheel',e=>{
    if(!e.ctrlKey && !e.metaKey) {
      e.preventDefault();const p=camera(), factor=e.deltaMode===1?16:e.deltaMode===2?view.clientHeight:1;
      panTo(p.left+(e.shiftKey && !e.deltaX?e.deltaY:e.deltaX)*factor,p.top+(e.shiftKey?0:e.deltaY)*factor);save();return;
    }
    e.preventDefault();
    const r=view.getBoundingClientRect();zoomBy(Math.exp(-e.deltaY*.008),e.clientX-r.left,e.clientY-r.top);
  },{passive:false});
  view.addEventListener('click',e=>{
    if(ignoreClick && e.detail!==0) {e.stopImmediatePropagation();e.preventDefault();ignoreClick=false;}
  },true);
  function paint() {
    frame=0;
    if(!drag?.moving) return;
    const d=drag;
    if(!d.id) {panTo(d.left-d.x+d.sx,d.top-d.y+d.sy);return;}
    const rect=view.getBoundingClientRect();
    const auto=(pos,min,max)=>pos<min+20?-8:pos>max-20?8:0;
    const vx=auto(d.x,rect.left,rect.right),vy=auto(d.y,rect.top,rect.bottom);
    if(vx || vy) view.scrollBy(vx,vy);
    const dx=(d.x-d.sx+camera().left-d.left)/getScale(),dy=(d.y-d.sy+camera().top-d.top)/getScale();
    move(d.id,d.start,dx,dy);
    if(vx || vy) frame=requestAnimationFrame(paint);
  }
  view.onpointerdown=e=>{
    ignoreClick=false;
    if(!e.isPrimary || ![0,1].includes(e.button) || (!space && e.button===0 && e.target.closest('[data-branch],[data-reset-card],[data-folder]'))) return;
    const id=!space && e.button===0 && e.target.closest('[data-vertex]')?.dataset.vertex;
    if(e.button===1 || space) e.preventDefault();
    drag={pid:e.pointerId,id,start:id && startNode(id),sx:e.clientX,sy:e.clientY,x:e.clientX,y:e.clientY,...camera(),moving:false};
    // Keep click targeting the original button even if the pointer leaves the paper.
    e.target.setPointerCapture(e.pointerId);busy(true);
  };
  view.onpointermove=e=>{
    if(!drag || e.pointerId!==drag.pid) return;
    drag.x=e.clientX;drag.y=e.clientY;
    if(!drag.moving && Math.hypot(e.clientX-drag.sx,e.clientY-drag.sy)<5) return;
    if(!drag.moving) {drag.moving=true;view.classList.add('is-dragging');if(drag.id) select(drag.id);}
    if(!frame) frame=requestAnimationFrame(paint);
  };
  const finish=e=>{
    if(!drag || e.pointerId!==drag.pid) return;
    if(frame) {cancelAnimationFrame(frame);frame=0;}
    if(drag.moving) {
      paint();cancelAnimationFrame(frame);frame=0;ignoreClick=true;
      if(drag.id) endNode(drag.id,drag.start);else save();
    }
    drag=null;view.classList.remove('is-dragging');busy(false);
  };
  view.onpointerup=finish;view.onpointercancel=finish;view.onlostpointercapture=finish;
  let sizeDrag=null;
  grip.onpointerdown=e=>{
    if(e.button!==0 || !e.isPrimary) return;
    e.preventDefault();grip.focus({preventScroll:true});grip.setPointerCapture(e.pointerId);
    sizeDrag={id:e.pointerId,y:e.clientY,height:view.getBoundingClientRect().height};busy(true);
  };
  grip.onpointermove=e=>{if(sizeDrag?.id===e.pointerId) resize(sizeDrag.height+e.clientY-sizeDrag.y);};
  grip.onlostpointercapture=()=>{if(sizeDrag){sizeDrag=null;save();busy(false);}};
  grip.onkeydown=e=>{
    if(!['ArrowUp','ArrowDown','Home','End'].includes(e.key)) return;
    e.preventDefault();
    resize(e.key==='Home'?180:e.key==='End'?innerHeight:view.clientHeight+(e.key==='ArrowDown'?32:-32));save();
  };
}
