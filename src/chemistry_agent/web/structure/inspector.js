/* Read-only source data, separate from the transformed rendering scene. */
(function(root){
  'use strict';
  const number=value=>Number.isFinite(value)?Number(value.toFixed(6)).toString():'—';
  const mass=atom=>Number.isFinite(atom.mass)?number(atom.mass):Number.isFinite(StructureLammps.weights[atom.elem])?number(StructureLammps.weights[atom.elem])+' *':'—';
  function cellData(frame){
    const cell=frame.cell;
    if(!cell||cell.every(v=>v.every(x=>x===0)))return null;
    const lengths=cell.map(v=>Math.hypot(...v));
    const angle=(a,b)=>{
      const denominator=lengths[a]*lengths[b];
      return denominator?Math.acos(Math.max(-1,Math.min(1,cell[a].reduce((s,v,i)=>s+v*cell[b][i],0)/denominator)))*180/Math.PI:NaN;
    };
    const [a,b,c]=cell;
    return {vectors:cell,lengths,angles:[angle(1,2),angle(0,2),angle(0,1)],origin:frame.origin||[0,0,0],
      bounds:[0,1,2].map(i=>cell.reduce((sum,v)=>sum+Math.abs(v[i]),0)),
      volume:Math.abs(a[0]*(b[1]*c[2]-b[2]*c[1])-a[1]*(b[0]*c[2]-b[2]*c[0])+a[2]*(b[0]*c[1]-b[1]*c[0])),pbc:frame.pbc||[false,false,false]};
  }
  function types(atoms){
    const groups=new Map();
    for(const atom of atoms){
      const supplied=atom.type!=null&&atom.type!=='';
      // An element fallback is explicitly distinguished from a source type.
      const key=supplied?'type:'+atom.type:'element:'+atom.elem;
      if(!groups.has(key))groups.set(key,{type:supplied?String(atom.type):'—',name:atom.typeName||'',elements:new Set(),masses:new Set(),count:0});
      const group=groups.get(key);group.count++;group.elements.add(atom.elem);group.masses.add(mass(atom));
    }
    return [...groups.values()].sort((a,b)=>a.type.localeCompare(b.type,undefined,{numeric:true}));
  }
  function particles(frame,query='',selected=null){
    const needle=query.trim().toLowerCase(),indices=[];
    for(let i=0;i<frame.atoms.length;i++){
      const atom=frame.atoms[i];
      if(selected&&!selected.has(i))continue;
      if(needle&&![i+1,atom.id,atom.sourceId,atom.type,atom.typeName,atom.elem,atom.name,atom.residue,atom.resid].some(v=>v!=null&&String(v).toLowerCase().includes(needle)))continue;
      indices.push(i);
    }
    return indices;
  }
  class Panel {
    constructor(onResize,onSelect){
      this.$=id=>document.getElementById(id);this.onResize=onResize;this.onSelect=onSelect;
      this.tab='particles';this.page=0;this.limit=50;this.lastPaint=0;this.timer=null;
      this.$('inspector-toggle').onclick=()=>this.open(this.$('structure-inspector').hidden);
      this.$('inspector-close').onclick=()=>this.open(false);
      const tabs=[...document.querySelectorAll('[data-inspector-tab]')];
      for(const [index,button] of tabs.entries()){
        button.onclick=()=>{this.tab=button.dataset.inspectorTab;this.page=0;this.draw();};
        button.onkeydown=event=>{
          if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;
          event.preventDefault();const target=event.key==='Home'?0:event.key==='End'?tabs.length-1:(index+(event.key==='ArrowRight'?1:tabs.length-1))%tabs.length;
          tabs[target].click();tabs[target].focus();
        };
      }
      for(const id of ['particle-filter','inspector-selected','cell-mode'])this.$(id).addEventListener('input',()=>{this.page=0;this.draw();});
      this.$('inspector-previous').onclick=()=>{this.page--;this.draw();};
      this.$('inspector-next').onclick=()=>{this.page++;this.draw();};
    }
    open(visible){
      this.$('structure-inspector').hidden=!visible;
      this.$('inspector-toggle').setAttribute('aria-expanded',String(visible));
      if(visible)this.draw();else {clearTimeout(this.timer);this.timer=null;this.$('inspector-toggle').focus();}
      this.onResize();
    }
    clear(){this.frame=null;clearTimeout(this.timer);this.timer=null;this.$('inspector-content').replaceChildren();}
    update(frame,index,selection,force=false){
      this.frame=frame;this.index=index;this.selection=selection;
      if(this.$('structure-inspector').hidden)return;
      // At most four table refreshes per second, with a trailing refresh when
      // playback stops. Never construct a DOM row for every particle.
      if(force||performance.now()-this.lastPaint>=250){clearTimeout(this.timer);this.timer=null;this.draw();}
      else if(!this.timer)this.timer=setTimeout(()=>{this.timer=null;this.draw();},250);
    }
    table(headings,rows,indices=null){
      const table=document.createElement('table'),head=document.createElement('thead'),body=document.createElement('tbody'),header=document.createElement('tr');
      const numeric=heading=>/^(数量|质量|显示半径|X[（ /]|Y[（ /]|Z[（ /]|电荷)/.test(heading);
      for(const text of headings){const th=document.createElement('th');th.textContent=text;th.scope='col';if(numeric(text))th.className='numeric';header.append(th);}
      head.append(header);
      for(const [rowIndex,row] of rows.entries()){
        const tr=document.createElement('tr');
        for(const [column,value] of row.entries()){
          const td=document.createElement('td');
          // Cell row headings are UI labels; particle/type names and all numeric
          // values belong to the source structure and must remain unchanged.
          if(this.tab!=='cell'||column!==0)td.dataset.noI18n='';
          if(numeric(headings[column]))td.className='numeric';
          if(indices&&column===0){const button=document.createElement('button');button.textContent=value;button.onclick=()=>this.onSelect(indices[rowIndex]);td.append(button);}
          else td.textContent=String(value??'—');
          tr.append(td);
        }
        body.append(tr);
      }
      table.append(head,body);return table;
    }
    draw(){
      if(!this.frame||this.$('structure-inspector').hidden)return;
      this.lastPaint=performance.now();
      const isCell=this.tab==='cell',isParticles=this.tab==='particles';
      for(const button of document.querySelectorAll('[data-inspector-tab]')){
        const active=button.dataset.inspectorTab===this.tab;button.setAttribute('aria-selected',String(active));button.tabIndex=active?0:-1;
      }
      const content=this.$('inspector-content');content.setAttribute('aria-labelledby',this.tab+'-tab');
      this.$('particle-filter').hidden=!isParticles;this.$('inspector-selected').parentElement.hidden=!isParticles;
      this.$('cell-mode').hidden=!isCell;
      this.$('inspector-frame').textContent=`第 ${this.index+1} 帧`;
      let headings,rows,indices=null,note=this.transformed?'原始坐标（未含 Wrap/校正）':'';
      if(isCell){
        const cell=cellData(this.frame);headings=['属性','X / a','Y / b','Z / c'];rows=[];
        if(cell){
          rows=this.$('cell-mode').value==='parameters'?
            [['长度（Å）',...cell.lengths.map(number)],['角度 α / β / γ（°）',...cell.angles.map(number)]]:
            cell.vectors.map((v,i)=>[['a（Å）','b（Å）','c（Å）'][i],...v.map(number)]);
          rows.push(['原点（Å）',...cell.origin.map(number)],['包围盒（Å）',...cell.bounds.map(number)],['周期边界',...cell.pbc.map(v=>v?'✓':'—')],['体积（Å³）',number(cell.volume),'','']);
          note='';
        }else note='当前帧没有晶胞信息。';
      }else if(this.tab==='types'){
        headings=['类型','类型名称','元素','数量','质量（u）','显示半径（Å）'];
        rows=types(this.frame.atoms).map(group=>[group.type,group.name||'—',[...group.elements].join(' / '),group.count,[...group.masses].join(' / '),[...group.elements].map(elem=>number(StructureAppearance.radius(elem))).join(' / ')]);
        note='';
      }else{
        const atomList=this.frame.atoms,origin=this.frame.origin||[0,0,0];
        indices=particles(this.frame,this.$('particle-filter').value,this.$('inspector-selected').checked?new Set(this.selection):null);
        headings=['序号','ID','类型','元素','X（Å）','Y（Å）','Z（Å）','质量（u）','原子名','残基','电荷（e）'];
        rows=indices; // Materialize only the current page below.
        this.page=Math.max(0,Math.min(this.page,Math.ceil(rows.length/this.limit)-1));
        const start=this.page*this.limit,visible=indices.slice(start,start+this.limit);
        const values=visible.map(i=>{const a=atomList[i];return [i+1,a.sourceId??a.id??'—',a.type||'—',a.elem,...[a.x,a.y,a.z].map((v,j)=>number(v+origin[j])),mass(a),a.name||'—',a.residue?`${a.residue} ${a.resid??''}`:'—',number(a.charge)];});
        this.paint(headings,values,visible,rows.length,note);return;
      }
      this.page=Math.max(0,Math.min(this.page,Math.ceil(rows.length/this.limit)-1));
      this.paint(headings,rows.slice(this.page*this.limit,(this.page+1)*this.limit),indices,rows.length,note);
    }
    paint(headings,rows,indices,total,note){
      const content=this.$('inspector-content'),scroll=content.scrollLeft;
      content.replaceChildren(this.table(headings,rows,indices));content.scrollLeft=scroll;
      this.$('inspector-count').textContent=total?`${this.page*this.limit+1}–${Math.min(total,(this.page+1)*this.limit)} / ${total}`:'0';
      for(const id of ['inspector-previous','inspector-next'])this.$(id).hidden=this.tab==='cell'||total<=this.limit;
      this.$('inspector-previous').disabled=this.page===0;this.$('inspector-next').disabled=(this.page+1)*this.limit>=total;
      if(this.tab!=='cell' && rows.some(row=>String(row[this.tab==='types'?4:7]).includes('*')))note=[note,'* 元素参考质量'].filter(Boolean).join(' · ');
      this.$('inspector-note').textContent=note;
    }
  }
  const api={Panel,cellData,types,particles};
  if(typeof module!=='undefined'&&module.exports)module.exports=api;else root.StructureInspector=api;
})(typeof window!=='undefined'?window:globalThis);
