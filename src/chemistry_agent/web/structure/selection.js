/* Display rules and ordered measurements; never modify structure coordinates. */
(function(root) {
  'use strict';
  const sub=(a,b)=>['x','y','z'].map(k=>a[k]-b[k]);
  const dot=(a,b)=>a.reduce((n,x,i)=>n+x*b[i],0);
  const cross=(a,b)=>[a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]];
  const scale=(v,s)=>v.map(x=>x*s);
  function measure(atoms,indices) {
    if(indices.length<2||indices.length>4)return '';
    const [a,b,c,d]=indices.map(i=>atoms[i]);
    if(!a||!b)return '';
    if(indices.length===2)return `距离 1–2：${Math.hypot(...sub(a,b)).toFixed(4)} Å`;
    const u=sub(a,b),v=sub(c,b),length=Math.hypot(...u)*Math.hypot(...v);
    if(length<1e-12)return '角度未定义（原子重合）';
    if(indices.length===3)return `夹角 1–2–3：${(Math.acos(Math.max(-1,Math.min(1,dot(u,v)/length)))*180/Math.PI).toFixed(3)}°`;
    const axis=scale(v,1/Math.hypot(...v)),w=sub(d,c);
    const p=u.map((x,i)=>x-dot(u,axis)*axis[i]),q=w.map((x,i)=>x-dot(w,axis)*axis[i]);
    if(Math.hypot(...p)*Math.hypot(...q)<1e-12)return '二面角未定义（相邻三原子共线）';
    return `二面角 1–2–3–4：${(Math.atan2(dot(cross(axis,p),q),dot(p,q))*180/Math.PI).toFixed(3)}°`;
  }
  function validIds(frame) {
    const ids=frame.atoms.map(a=>a.id);
    return ids.every(id=>id!=null)&&new Set(ids).size===ids.length;
  }
  function compatible(a,b) {return a.atoms.length===b.atoms.length&&a.atoms.every((atom,i)=>atom.elem===b.atoms[i].elem);}
  function remap(a,b,indices) {
    if(validIds(a)&&validIds(b)) {
      const lookup=new Map(b.atoms.map((atom,i)=>[atom.id,i]));
      return indices.map(i=>lookup.get(a.atoms[i]?.id)).filter(i=>i!==undefined);
    }
    return compatible(a,b)?indices.filter(i=>i<b.atoms.length):[];
  }
  function toggleIndices(selected,hits) {
    const result=new Set(selected);
    for(const index of hits) {if(result.has(index))result.delete(index);else result.add(index);}
    return [...result];
  }
  function formula(atoms,indices) {
    const counts=new Map();for(const i of indices){const e=atoms[i].elem;counts.set(e,(counts.get(e)||0)+1);}
    const order='X H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni Cu Zn Ga Ge As Se Br Kr Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb Te I Xe Cs Ba La Ce Pr Nd Pm Sm Eu Gd Tb Dy Ho Er Tm Yb Lu Hf Ta W Re Os Ir Pt Au Hg Tl Pb Bi Po At Rn Fr Ra Ac Th Pa U Np Pu Am Cm Bk Cf Es Fm Md No Lr Rf Db Sg Bh Hs Mt Ds Rg Cn Nh Fl Mc Lv Ts Og'.split(' ');
    return [...counts].sort((a,b)=>order.indexOf(a[0])-order.indexOf(b[0])).map(([e,n])=>(n===1?'':n)+e).join('+');
  }
  class DisplayState {
    constructor(){this.hiddenElements=new Set();this.overrides=new Map();}
    apply(frame,indices,patch){for(const i of indices)this.overrides.set(i,{...this.overrides.get(i),...patch});}
    reset(frame,indices){for(const i of indices)this.overrides.delete(i);}
    transition(a,b){
      const next=new Map();
      for(const [i,rule] of this.overrides){const mapped=remap(a,b,[i]);if(mapped.length)next.set(mapped[0],rule);}
      this.overrides=next;
    }
    resolve(atom,index,defaults){
      const rule=this.overrides.get(index)||{};
      return {...defaults,...rule,visible:!this.hiddenElements.has(atom.elem)&&rule.visible!==false};
    }
  }
  function inRectangle(points,start,end) {
    const minX=Math.min(start.x,end.x),maxX=Math.max(start.x,end.x),minY=Math.min(start.y,end.y),maxY=Math.max(start.y,end.y);
    return points.filter(p=>Number.isFinite(p.x)&&Number.isFinite(p.y)&&p.x>=minX&&p.x<=maxX&&p.y>=minY&&p.y<=maxY).map(p=>p.index);
  }
  function outlineRing(center,radius,normal,width,cameraDistance=Infinity,segments=48) {
    // Camera-facing silhouette ring, not a sphere shell. Its plane remains at
    // the tangent circle, so the normal WebGL depth buffer handles occlusion.
    const n=normal.map(v=>v/Math.hypot(...normal));
    const u=cross(Math.abs(n[2])<.9?[0,0,1]:[0,1,0],n);
    const length=Math.hypot(...u);for(let i=0;i<3;i++)u[i]/=length;
    const v=cross(n,u),offset=Number.isFinite(cameraDistance)?radius*radius/Math.max(radius*1.01,cameraDistance):0;
    const silhouette=Math.sqrt(Math.max(0,radius*radius-offset*offset));
    const vertexArr=[],normalArr=[],faceArr=[];
    for(let step=0;step<=segments;step++)for(const r of [silhouette*.998,silhouette+width]) {
      const theta=step*2*Math.PI/segments;
      const point=n.map((value,i)=>[center.x,center.y,center.z][i]+value*offset+r*(u[i]*Math.cos(theta)+v[i]*Math.sin(theta)));
      vertexArr.push({x:point[0],y:point[1],z:point[2]});normalArr.push({x:n[0],y:n[1],z:n[2]});
    }
    for(let step=0;step<segments;step++){const i=step*2;faceArr.push(i,i+1,i+2,i+1,i+3,i+2);}
    return {vertexArr,normalArr,faceArr,color:0x004500,opacity:1};
  }
  const api={measure,remap,DisplayState,inRectangle,outlineRing,toggleIndices,formula};
  if(typeof module!=='undefined'&&module.exports)module.exports=api;else root.StructureSelection=api;
})(typeof window!=='undefined'?window:globalThis);
