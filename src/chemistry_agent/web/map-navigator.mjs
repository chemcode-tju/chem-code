// A single path and a viewport rectangle represent the whole graph, even when it contains hundreds of cards.
export function createNavigator(button, view, read) {
  const svg=button.querySelector('svg'), dots=svg.querySelector('path'), viewport=svg.querySelector('rect');
  function position() {
    const {layout,scale}=read();
    if(!layout) return;
    const w=Math.min(layout.width,view.clientWidth/scale),h=Math.min(layout.height,view.clientHeight/scale);
    viewport.setAttribute('width',w);viewport.setAttribute('height',h);
    viewport.setAttribute('x',Math.max(0,Math.min(layout.width-w,view.scrollLeft/scale)));
    viewport.setAttribute('y',Math.max(0,Math.min(layout.height-h,view.scrollTop/scale)));
  }
  function draw() {
    const {layout,width,height}=read();
    svg.setAttribute('viewBox',`0 0 ${layout.width} ${layout.height}`);
    const cards=[...layout.points.values()].map(p=>`M${p.x} ${p.y}h${width}v${height}h-${width}z`);
    for(const header of layout.headers.values()) cards.push(`M${header.x} ${header.y}h${width}v18h-${width}z`);
    dots.setAttribute('d',cards.join(''));
    position();
  }
  function pan(e) {
    const r=svg.getBoundingClientRect(), {layout,scale}=read();
    view.scrollTo((e.clientX-r.left)/r.width*layout.width*scale-view.clientWidth/2,
      (e.clientY-r.top)/r.height*layout.height*scale-view.clientHeight/2);
  }
  button.onpointerdown=e=>{if(e.button===0){button.setPointerCapture(e.pointerId);pan(e);}};
  button.onpointermove=e=>{if(button.hasPointerCapture(e.pointerId)) pan(e);};
  button.onkeydown=e=>{
    const delta={ArrowUp:[0,-1],ArrowDown:[0,1],ArrowLeft:[-1,0],ArrowRight:[1,0]}[e.key];
    if(delta) {e.preventDefault();e.stopPropagation();view.scrollBy(delta[0]*view.clientWidth*.7,delta[1]*view.clientHeight*.7);}
  };
  return {draw,position};
}
