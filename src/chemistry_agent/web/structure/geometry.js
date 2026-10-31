/* Display-only periodic geometry. Original document coordinates remain untouched. */
(function(root) {
  'use strict';
  const point = a => [a.x,a.y,a.z];
  const dot = (a,b) => a.reduce((sum,x,i) => sum+x*b[i],0);
  const cross = (a,b) => [a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]];
  const sub = (a,b) => a.map((x,i) => x-b[i]);
  function basis(cell) {
    if (!cell || cell.length !== 3) throw Error('结构没有晶胞，无法进行周期操作');
    const det = dot(cell[0],cross(cell[1],cell[2]));
    if (Math.abs(det)<1e-10) throw Error('晶胞体积为零，无法进行周期操作');
    const reciprocal = [cross(cell[1],cell[2]),cross(cell[2],cell[0]),cross(cell[0],cell[1])].map(v=>v.map(x=>x/det));
    return {fractional:p=>reciprocal.map(v=>dot(p,v)), cartesian:f=>[0,1,2].map(j=>dot(f,cell.map(v=>v[j]))), reciprocal};
  }
  function wrap(frame) {
    const b=basis(frame.cell), periodic=frame.pbc || [true,true,true];
    return {...frame,atoms:frame.atoms.map(atom=>{
      const f=b.fractional(point(atom)).map((x,i)=>periodic[i] ? x-Math.floor(x+1e-10) : x);
      const [x,y,z]=b.cartesian(f);return {...atom,x,y,z};
    })};
  }
  function minimumImage(delta, cell, pbc, cutoff) {
    if (!cell || !pbc?.some(Boolean)) return Math.hypot(...delta)<=cutoff ? delta : null;
    const b=basis(cell), f=b.fractional(delta);
    // Reciprocal-vector bounds include every lattice image within the cutoff,
    // including skewed cells where simply rounding fractional coordinates fails.
    const limits=f.map((x,i)=>pbc[i] ? [Math.ceil(x-cutoff*Math.hypot(...b.reciprocal[i])-1e-9),Math.floor(x+cutoff*Math.hypot(...b.reciprocal[i])+1e-9)] : [0,0]);
    if (limits.reduce((n,[a,z])=>n*Math.max(0,z-a+1),1)>4096) throw Error('晶胞过小或过度倾斜，氢键周期搜索范围过大');
    let best=null, length=cutoff*cutoff+1e-10;
    for(let i=limits[0][0];i<=limits[0][1];i++)for(let j=limits[1][0];j<=limits[1][1];j++)for(let k=limits[2][0];k<=limits[2][1];k++) {
      const v=sub(delta,b.cartesian([i,j,k])), d=dot(v,v);
      if(d<length){best=v;length=d;}
    }
    return best;
  }
  function hydrogenBonds(frame, options={}) {
    const maxDA=options.distance ?? 3.5, minAngle=options.angle ?? 150;
    const heavy=frame.atoms.map((a,i)=>({a,i})).filter(({a})=>['N','O','F'].includes(a.elem));
    const bonds=[];
    for (const [hi,h] of frame.atoms.entries()) {
      if(h.elem!=='H')continue;
      let donor=null, donorVector=null, nearest=1.3;
      for(const item of heavy){const v=minimumImage(sub(point(item.a),point(h)),frame.cell,frame.pbc,1.3);if(v&&Math.hypot(...v)<nearest&&Math.hypot(...v)>.2){donor=item;donorVector=v;nearest=Math.hypot(...v);}}
      if(!donor)continue;
      for(const {a,i} of heavy){
        if(i===donor.i)continue;
        const ha=minimumImage(sub(point(a),point(h)),frame.cell,frame.pbc,2.5);
        if(!ha||Math.hypot(...ha)<.3||Math.hypot(...sub(ha,donorVector))>maxDA)continue;
        const angle=Math.acos(Math.max(-1,Math.min(1,dot(donorVector,ha)/(nearest*Math.hypot(...ha)))))*180/Math.PI;
        if(angle>=minAngle)bonds.push({donor:donor.i,hydrogen:hi,acceptor:i,start:point(h),end:point(h).map((x,j)=>x+ha[j])});
      }
    }
    return bonds;
  }
  // Standard atomic masses from ASE; explicit per-particle mass takes precedence.
  const atomicMasses={"H":1.008,"He":4.002602,"Li":6.94,"Be":9.0121831,"B":10.81,"C":12.011,"N":14.007,"O":15.999,"F":18.998403163,"Ne":20.1797,"Na":22.98976928,"Mg":24.305,"Al":26.9815385,"Si":28.085,"P":30.973761998,"S":32.06,"Cl":35.45,"Ar":39.948,"K":39.0983,"Ca":40.078,"Sc":44.955908,"Ti":47.867,"V":50.9415,"Cr":51.9961,"Mn":54.938044,"Fe":55.845,"Co":58.933194,"Ni":58.6934,"Cu":63.546,"Zn":65.38,"Ga":69.723,"Ge":72.63,"As":74.921595,"Se":78.971,"Br":79.904,"Kr":83.798,"Rb":85.4678,"Sr":87.62,"Y":88.90584,"Zr":91.224,"Nb":92.90637,"Mo":95.95,"Tc":97.90721,"Ru":101.07,"Rh":102.9055,"Pd":106.42,"Ag":107.8682,"Cd":112.414,"In":114.818,"Sn":118.71,"Sb":121.76,"Te":127.6,"I":126.90447,"Xe":131.293,"Cs":132.90545196,"Ba":137.327,"La":138.90547,"Ce":140.116,"Pr":140.90766,"Nd":144.242,"Pm":144.91276,"Sm":150.36,"Eu":151.964,"Gd":157.25,"Tb":158.92535,"Dy":162.5,"Ho":164.93033,"Er":167.259,"Tm":168.93422,"Yb":173.054,"Lu":174.9668,"Hf":178.49,"Ta":180.94788,"W":183.84,"Re":186.207,"Os":190.23,"Ir":192.217,"Pt":195.084,"Au":196.966569,"Hg":200.592,"Tl":204.38,"Pb":207.2,"Bi":208.9804,"Po":208.98243,"At":209.98715,"Rn":222.01758,"Fr":223.01974,"Ra":226.02541,"Ac":227.02775,"Th":232.0377,"Pa":231.03588,"U":238.02891,"Np":237.04817,"Pu":244.06421,"Am":243.06138,"Cm":247.07035,"Bk":247.07031,"Cf":251.07959,"Es":252.083,"Fm":257.09511,"Md":258.09843,"No":259.101,"Lr":262.11,"Rf":267.122,"Db":268.126,"Sg":271.134,"Bh":270.133,"Hs":269.1338,"Mt":278.156,"Ds":281.165,"Rg":281.166,"Cn":285.177,"Nh":286.182,"Fl":289.19,"Mc":289.194,"Lv":293.204,"Ts":293.208,"Og":294.214};
  function centerOfMass(atoms,indices) {
    if(!indices.length)throw Error('质心校正至少需要一个原子');
    let total=0;const center=[0,0,0];
    for(const i of indices) {
      const atom=atoms[i],mass=atom.mass??atomicMasses[atom.elem]??1;
      if(!Number.isFinite(mass)||mass<=0)throw Error('原子质量必须为正数');
      total+=mass;['x','y','z'].forEach((axis,j)=>center[j]+=mass*atom[axis]);
    }
    return center.map(v=>v/total);
  }
  function removeDrift(frame,reference,currentIndices,referenceIndices) {
    if(currentIndices.length!==referenceIndices.length)throw Error('质心校正的原子无法与参考帧对应');
    const current=centerOfMass(frame.atoms,currentIndices),target=centerOfMass(reference.atoms,referenceIndices);
    const shift=target.map((v,i)=>v-current[i]);
    return {...frame,atoms:frame.atoms.map(atom=>({...atom,x:atom.x+shift[0],y:atom.y+shift[1],z:atom.z+shift[2]})),driftShift:shift};
  }
  function followAtom(frame, anchor) {
    if(!anchor)return null;
    if(anchor.id!=null&&frame.atoms.filter(a=>a.id===anchor.id).length!==1)throw Error('跟随原子 ID 缺失或重复');
    const index=anchor.id!=null ? frame.atoms.findIndex(a=>a.id===anchor.id) : anchor.index;
    if(index<0||!frame.atoms[index]||frame.atoms[index].elem!==anchor.element||
      (anchor.id==null&&frame.atoms.map(a=>a.elem).join(',')!==anchor.signature)) throw Error('跟随原子在当前帧中无法对应；请重新指定原子');
    return frame.atoms[index];
  }
  const api={basis,wrap,minimumImage,hydrogenBonds,followAtom,centerOfMass,removeDrift};
  if(typeof module!=='undefined'&&module.exports)module.exports=api;else root.StructureGeometry=api;
})(typeof window!=='undefined'?window:globalThis);
