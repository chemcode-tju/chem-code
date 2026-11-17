/* Pure input/layout helpers plus nonmodal draggable settings panels. */
const StructureControls=(()=>{
  function atomIndices(text,count){
    const indices=new Set();
    for(const token of text.trim().split(/[\s,，]+/).filter(Boolean)){
      const match=/^(\d+)(?:-(\d+))?$/.exec(token);
      if(!match)throw Error('原子序号请输入 1, 3, 5-12 这样的格式');
      const start=Number(match[1]),end=Number(match[2]||match[1]);
      if(start<1||end<start||end>count)throw Error(`原子序号必须在 1–${count} 之间`);
      for(let i=start;i<=end;i++)indices.add(i-1);
    }
    if(!indices.size)throw Error('请输入原子序号，或点击“填入当前选择”');
    return [...indices];
  }
  function referenceIndex(value,count){
    const frame=Number(value);
    if(!Number.isInteger(frame)||frame<1||frame>count)throw Error('参考帧尚未加载或超出范围');
    return frame-1;
  }
  function timelineTicks(count,width,measure){
    if(count<1||width<=0)return [];
    const firstWidth=measure('1'),lastWidth=measure(String(count)),gap=12;
    // Stems align with the 12px thumb. Edge labels slide inward independently.
    const start=Math.min(6,width/2),end=width-start;
    const first={value:1,x:Math.max(start,firstWidth/2),tickX:start};
    const last={value:count,x:Math.max(width/2,Math.min(end,width-lastWidth/2)),tickX:end};
    if(count===1)return [first];
    const limit=last.x-lastWidth/2-gap;
    let edge=first.x+firstWidth/2;
    if(edge>limit)return [last];
    const span=end-start;
    const rough=Math.max(1,(count-1)*(lastWidth+gap)/Math.max(1,span));
    const base=10**Math.floor(Math.log10(rough));
    const step=[1,2,5,10].find(v=>v*base>=rough)*base;
    const marks=[first];
    for(let n=step;n<count;n+=step){
      if(n<=1)continue;
      const x=start+(n-1)/(count-1)*span,w=measure(String(n));
      if(x-w/2>=edge+gap && x+w/2<=limit){marks.push({value:n,x,tickX:x});edge=x+w/2;}
    }
    marks.push(last);return marks;
  }
  function draggable(panel){
    const handle=panel.querySelector('h2');let start=null;
    handle.addEventListener('pointerdown',event=>{
      if(event.button!==0)return;
      const rect=panel.getBoundingClientRect();start={x:event.clientX,y:event.clientY,left:rect.left,top:rect.top};
      handle.setPointerCapture(event.pointerId);event.preventDefault();
    });
    handle.addEventListener('pointermove',event=>{
      if(!start)return;
      panel.style.left=Math.max(0,Math.min(innerWidth-panel.offsetWidth,start.left+event.clientX-start.x))+'px';
      panel.style.top=Math.max(0,Math.min(innerHeight-40,start.top+event.clientY-start.y))+'px';panel.style.right='auto';
    });
    for(const name of ['pointerup','pointercancel'])handle.addEventListener(name,()=>start=null);
  }
  return {atomIndices,referenceIndex,timelineTicks,draggable};
})();
if(typeof module!=='undefined')module.exports=StructureControls;
