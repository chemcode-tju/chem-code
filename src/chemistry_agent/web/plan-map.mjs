import {escapeHtml as esc} from './core.mjs';
import {CARD, buildGraph, clearDrop, edgePath, edgeRoute, kindName, layoutGraph, overviewFolds, taskLabel} from './plan-layout.mjs';
import {bindMapGestures} from './map-gestures.mjs';
import {createNavigator} from './map-navigator.mjs';
import {t} from './i18n.mjs';
import {planKanban} from './plan-kanban.mjs';
import {taskAgentBar} from './task-agent-control.mjs';

const icon = id => `<svg aria-hidden="true"><use href="/assets/ui-icons.svg#i-${id}"/></svg>`;
const tool = (attr,label,content) => `<button type="button" ${attr} aria-label="${label}" title="${label}">${content}</button>`;
const shortcut = (keys,label) => `<span><kbd>${keys}</kbd> <span>${label}</span></span>`;
const serial = value => esc(JSON.stringify(value));

export function planMap(layers, key, attribute, title = '', controls = '', showAttention = false) {
  const nodes = layers.flat();
  if (!nodes.length) return '';
  const cards = nodes.map(n => {
    const label=taskLabel(n), split=n.title===n.id && label!==n.id ? label.lastIndexOf(' · ') : -1;
    const kind=n.kind || 'vasp';
    const caption=n.groupPath?.join(' / ') || (split<0?'':label.slice(0,split));
    return `<div class="map-position" data-vertex="${esc(n.id)}" data-label="${esc(label)}" data-search="${esc(n.searchText || '')}" data-group-path="${serial(n.groupPath ?? null)}" data-parents="${serial(n.dependencies)}" ${n.auxiliary?'data-aux':''} hidden>
    ${caption?`<span class="map-card-caption" data-no-i18n title="${esc(caption)}">${esc(caption)}</span>`:''}
    <button type="button" class="map-node" ${attribute}="${esc(n.id)}" data-kind="${esc(kind)}" data-state="${esc(n.state)}" aria-label="${esc([label,n.status].filter(Boolean).join(' · '))}" title="${esc([kindName(kind),caption,label,n.status,n.description,n.jobSummary].filter(Boolean).join(' · '))}" tabindex="-1">
      <strong data-no-i18n>${esc(split<0?label:label.slice(split+3))}</strong><small class="map-card-status"><i aria-hidden="true"></i>${esc(n.status)}${attribute==='data-open-step'?'<span data-ionic-step></span>':''}</small>
    </button>${taskAgentBar(n)}<button type="button" data-reset-card aria-label="恢复这张卡片原位" hidden>${icon('reset')}<span>归位</span></button><button type="button" data-branch="${esc(n.id)}" tabindex="-1" hidden></button></div>`;
  }).join('');
  return `<div class="plan-map" data-map="${esc(key)}"><div class="map-board" role="region" aria-label="方案画布">
    <div class="map-tools"><div class="plan-view-switch" role="group" aria-label="方案视图"><button type="button" data-plan-view="kanban" aria-pressed="false">看板</button><button type="button" data-plan-view="canvas" aria-pressed="true">画布</button>${showAttention ? '<button type="button" data-plan-view="attention" aria-pressed="false">待处理 <span data-attention-count></span></button>' : ''}</div><div class="map-identity"><span data-map-count title="已展开 / 总步骤"></span></div>${controls}<div class="map-actions">
      ${tool('data-command="options" aria-expanded="false"','画布选项',icon('more-vertical'))}
    </div></div>
    <div class="map-pop" data-pop="options" hidden>
      <label data-board-option><input type="checkbox" data-board-rows> 上下两行</label>
      <button type="button" class="map-direction" data-command="direction" title="切换布局方向">从上到下 ↓</button>
      <label data-aux-option><input type="checkbox" data-all> 显示建模与分析步骤</label>
      <label><input type="checkbox" data-all-arrows> 显示全部方向箭头</label>
      <button type="button" data-command="overview">分支概览</button><button type="button" data-command="unfold">展开全部节点</button>
      <button type="button" data-command="arrange">恢复自动布局</button><label class="map-nav-option"><input type="checkbox" data-show-nav> 缩略图</label></div>
    ${planKanban(nodes,attribute)}
    ${showAttention ? '<section class="plan-attention" data-plan-attention aria-label="待处理事项" hidden></section>' : ''}
    <div class="map-window" tabindex="0" aria-label="计算依赖图"><div class="map-size"><div class="map-world"><svg class="map-edges" aria-hidden="true"></svg>${cards}</div></div></div>
    <button type="button" class="map-navigator" title="缩略图导航" aria-label="缩略图导航"><svg preserveAspectRatio="none" aria-hidden="true"><path/><rect/></svg></button>
    <div class="map-foot"><button type="button" data-command="keys" aria-expanded="false" title="画布快捷键">${icon('keys')}<span>快捷键</span></button><div class="map-zoom">${tool('data-zoom="-1"','缩小方案',icon('minus'))}${tool('data-zoom="0"','还原缩放','100%')}${tool('data-zoom="1"','放大方案',icon('plus'))}${tool('data-command="fit"','适应画布',icon('fit'))}${tool('data-command="arrange"','恢复自动布局',icon('reset'))}</div></div>
    <div class="map-pop map-help" data-pop="keys" hidden>
      ${shortcut('← / →','收起 / 展开分支')}${shortcut('↑ / ↓','上一个 / 下一个节点')}${shortcut('Enter','查看步骤详情')}${shortcut('R','恢复这张卡片原位')}${shortcut('0','适应画布')}${shortcut('Esc','关闭浮层')}${shortcut('Space + 拖动','平移画布')}
    </div><div class="map-grip" role="separator" tabindex="0" aria-orientation="horizontal" aria-label="调整画布高度" title="拖动调整画布高度"><i></i></div>
    </div></div>`;
}

const mounted = new WeakMap();
const bound = (v,min,max) => Math.max(min,Math.min(max,v));
function stored(key) { try {return JSON.parse(sessionStorage.getItem(key));} catch {return null;} }

function mount(map, previous) {
  const $ = selector=>map.querySelector(selector);
  const storageKey='chemistry.planView.'+map.dataset.map;
  const previousView=previous || stored(storageKey) || {};
  // Positions from the nested-folder canvas do not fit the new grid/card geometry.
  const saved=previousView.layoutVersion===6?previousView:{};
  const board=$('.map-board'), view=$('.map-window'), world=$('.map-world'), size=$('.map-size'), svg=$('.map-edges');
  const kanban=$('.plan-kanban');
  const attention=$('[data-plan-attention]');
  let viewMode=attention && saved.viewMode==='attention'?'attention':saved.viewMode==='canvas'?'canvas':'kanban';
  let boardRows=saved.boardRows!==false;
  kanban.dataset.rows=boardRows?'two':'one';
  $('[data-board-rows]').checked=boardRows;
  $('[data-board-rows]').onchange=event=>{boardRows=event.target.checked;kanban.dataset.rows=boardRows?'two':'one';persist();save();};
  let canvasCamera={left:saved.left,top:saved.top};
  let boardScroll={left:saved.boardLeft || 0,top:saved.boardTop || 0};
  const grip=$('.map-grip');
  const card=world.querySelector('.task-agent-bar') ? {...CARD,height:114,row:170} : CARD;
  world.style.setProperty('--node-w',card.width+'px');world.style.setProperty('--node-h',card.height+'px');
  const nodes=[...world.querySelectorAll('[data-vertex]')].map(el=>({id:el.dataset.vertex,title:el.dataset.label,searchText:el.dataset.search,
    parents:JSON.parse(el.dataset.parents),groupPath:JSON.parse(el.dataset.groupPath),caption:el.querySelector('.map-card-caption'),reset:el.querySelector('[data-reset-card]'),auxiliary:el.hasAttribute('data-aux'),el,button:el.querySelector('.map-node'),toggle:el.querySelector('[data-branch]')}));
  const byId=new Map(nodes.map(n=>[n.id,n])), onlyAux=nodes.every(n=>n.auxiliary);
  const graphs=new Map(), modes={...saved.modes};
  let all=saved.all!==false || onlyAux, graph, folds, moves, layout, active=null, lit=null, paths=[], incident;
  let showNav=saved.showNav!==false;
  let allArrows=saved.allArrows===true;
  let direction=saved.direction || '';
  const orientation=()=>direction || 'TB';
  let originX=0, originY=0;
  const camera=()=>({left:view.scrollLeft-originX,top:view.scrollTop-originY});
  const nav=createNavigator($('.map-navigator'),view,()=>({layout,scale,...camera(),...card}),panTo);
  let scale=bound(Number(saved.scale)||1,.005,3), height=saved.height || 0, pop='', raf=0, interacted=false;
  function initMode() {
    if (!graphs.has(all)) graphs.set(all,buildGraph(nodes,all));
    graph=graphs.get(all);
    const state=modes[all] || {};
    folds=new Set((state.folds || (graph.vertices.length>32 ? [...overviewFolds(graph)] : [])).filter(id=>graph.byId.has(id)));
    moves=new Map((state.moves || []).filter(([id,p])=>graph.byId.has(id) && Number.isFinite(p?.x) && Number.isFinite(p?.y)));
    $('[data-all]').checked=all; $('[data-aux-option]').hidden=onlyAux || nodes.every(n=>!n.auxiliary);
  }
  function snapshot() {
    modes[all]={folds:[...folds],moves:[...moves]};
    const focused=board.contains(document.activeElement);
    return {layoutVersion:6,modes,all,active:interacted?active:null,scale,height,showNav,allArrows,direction,boardRows,
      ...(viewMode==='canvas'?camera():canvasCamera),viewMode,
      boardLeft:viewMode==='kanban'?kanban.scrollLeft:boardScroll.left,
      boardTop:viewMode==='kanban'?kanban.scrollTop:boardScroll.top,focused};
  }
  function persist() {
    try {const {focused,...state}=snapshot();sessionStorage.setItem(storageKey,JSON.stringify(state));} catch { /* Optional. */ }
  }
  function save() {
    if (!board.getClientRects().length) return;
    cancelAnimationFrame(raf);
    raf=requestAnimationFrame(()=>{if(!map.isConnected) return;
      persist();
    });
  }
  function panTo(left,top) {
    const w=view.clientWidth, h=view.clientHeight;
    if (!w || !h) return;
    const graphWidth=Math.ceil(layout.width*scale), graphHeight=Math.ceil(layout.height*scale);
    // Keep scrollable paper on both sides of the graph, extending it as the camera moves.
    originX=Math.max(w,-Math.floor(left)+w);
    originY=Math.max(h,-Math.floor(top)+h);
    world.style.left=originX+'px';world.style.top=originY+'px';
    size.style.width=(originX+Math.max(graphWidth,Math.ceil(left)+w)+w)+'px';
    size.style.height=(originY+Math.max(graphHeight,Math.ceil(top)+h)+h)+'px';
    view.scrollTo(originX+left,originY+top);
    view.style.setProperty('--lane-inset',(left/scale+12)+'px');
    nav.position();
  }
  function setSize() {
    const {left,top}=camera();
    world.style.width=layout.width+'px';world.style.height=layout.height+'px';
    world.style.transform=`scale(${scale})`;map.dataset.scale=scale;
    $('[data-zoom="0"]').textContent=Math.round(scale*100)+'%';
    $('.map-navigator').hidden=viewMode!=='canvas' || !showNav || view.clientWidth<600;
    panTo(left,top);
  }
  function mark(id) {
    if (id===lit) return;
    function apply(key,on) {
      if(!key) return;
      for(const edge of incident?.get(key) || []) {
        edge.path.classList.toggle('is-traced',on);
        updateArrow(edge);
        const other=edge.source===key ? edge.target : edge.source;
        byId.get(other)?.el?.classList.toggle('is-neighbor',on);
      }
    }
    apply(lit,false); lit=id; apply(id,true);
  }
  function updateArrow(edge) {
    const traced=edge.path.classList.contains('is-traced');
    if(traced || allArrows) edge.path.setAttribute('marker-end',`url(#${edge.marker}${traced?'-active':''})`);
    else edge.path.removeAttribute('marker-end');
  }
  function edgeD(edge) {
    return edgePath(layout.points.get(edge.source),layout.points.get(edge.target),edgeRoute(edge.source,edge.target,layout,card),card);
  }
  function draw() {
    mark(null); layout=layoutGraph(graph,folds,moves,null,orientation(),card);
    const first=layout.points.keys().next().value;
    if (!layout.points.has(active)) active=first;
    board.dataset.direction=orientation();
    const directionButton=$('[data-command="direction"]');
    directionButton.innerHTML=`<span>${esc(t(orientation()==='LR'?'从左到右':'从上到下'))}</span>${icon('arrow-right')}`;
    directionButton.querySelector('svg').style.transform=orientation()==='LR'?'':'rotate(90deg)';
    paths=[];incident=new Map();
    const captions=new Set();
    for (const n of nodes) {
      const p=layout.points.get(n.id);
      n.el.hidden=!p; if (!p) continue;
      if(n.caption) {const text=n.caption.textContent;n.caption.hidden=captions.has(text);captions.add(text);}
      n.reset.hidden=!moves.has(n.id);
      n.el.style.left=p.x+'px';n.el.style.top=p.y+'px';n.button.tabIndex=n.id===active?0:-1;
      n.el.classList.toggle('is-current',interacted && n.id===active);
      if(interacted && n.id===active) n.button.setAttribute('aria-current','step');else n.button.removeAttribute('aria-current');
      const vertex=graph.byId.get(n.id), children=vertex.children;
      n.toggle.hidden=!children.length;
      if (children.length) {
        const folded=folds.has(n.id);
        n.toggle.setAttribute('aria-expanded',String(!folded));
        const label=(folded?t('展开分支'):t('收起分支'))+' · '+n.title+' ('+children.length+')';
        n.toggle.setAttribute('aria-label',label);n.toggle.title=label;
        n.toggle.textContent=(folded?'+':'−')+' '+children.length;
      }
      for (const source of vertex.parents) if(layout.points.has(source) && !folds.has(source)) paths.push({source,target:n.id,secondary:source!==vertex.primary});
    }
    const marker='map-arrow-'+map.dataset.map.replace(/[^a-zA-Z0-9_-]/g,'-');
    svg.innerHTML='<defs>'+['','-active'].map(suffix=>`<marker id="${marker}${suffix}" class="${suffix?'active-arrow':''}" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="5" markerHeight="5" orient="auto-start-reverse"><polygon points="1,1 7,4 1,7"/></marker>`).join('')+'</defs>'+paths.map(e=>`<path data-source="${esc(e.source)}" data-target="${esc(e.target)}" d="${edgeD(e)}"/>`).join('');
    for (const [i,path] of [...svg.querySelectorAll('path[data-source]')].entries()) {
      const e=paths[i];e.path=path;e.marker=marker;updateArrow(e);
      for (const id of [e.source,e.target]) {if(!incident.has(id)) incident.set(id,[]);incident.get(id).push(e);}
    }
    const tally=t((layout.points.size===graph.vertices.length?graph.vertices.length:layout.points.size+' / '+graph.vertices.length)+' 步');
    $('[data-map-count]').textContent=tally;
    updateKanban();
    setSize();nav.draw();if(interacted) mark(active);
  }
  function updateKanban() {
    let total=0;
    for(const column of kanban.querySelectorAll('[data-column]')) {
      let count=0;
      for(const row of column.querySelectorAll('[data-board-node]')) {
        const node=graph.byId.get(row.dataset.boardNode);
        row.hidden=!node;
        if(!row.hidden) count++;
      }
      column.querySelector('[data-column-count]').textContent=count;
      column.hidden=!count && ['ended','other'].includes(column.dataset.column);
      total+=count;
    }
    kanban.style.setProperty('--board-columns',Math.ceil([...kanban.querySelectorAll('.kanban-column')].filter(column=>!column.hidden).length/2));
    if(viewMode==='kanban') $('[data-map-count]').textContent=t(total+' 步');
    $('[data-command="options"]').hidden=viewMode==='attention';
  }
  function showView(mode) {
    if(viewMode==='canvas') canvasCamera=camera();
    if(viewMode==='kanban' && !kanban.hidden) boardScroll={left:kanban.scrollLeft,top:kanban.scrollTop};
    closePop();viewMode=mode;
    board.dataset.view=mode;
    board.setAttribute('aria-label',t({kanban:'方案看板',canvas:'方案画布',attention:'待处理事项'}[mode]));
    kanban.hidden=mode!=='kanban';view.hidden=mode!=='canvas';
    if(attention)attention.hidden=mode!=='attention';
    $('.map-identity').hidden=mode==='attention';
    $('.map-foot').hidden=mode!=='canvas';
    $('.map-grip').hidden=mode!=='canvas';
    for(const button of board.querySelectorAll('[data-plan-view]')) button.setAttribute('aria-pressed',String(button.dataset.planView===mode));
    // The auxiliary-step filter is shared; geometry controls belong to the canvas.
    for(const control of board.querySelectorAll('[data-pop="options"] > :not([data-aux-option]):not([data-board-option])')) control.hidden=mode!=='canvas';
    $('[data-board-option]').hidden=mode!=='kanban';
    const options=$('[data-command="options"]'),optionsLabel=mode==='kanban'?'看板选项':'画布选项';
    options.title=optionsLabel;options.setAttribute('aria-label',optionsLabel);
    draw();
    if(mode==='canvas') {
      const first=layout.points.get(active) || layout.points.values().next().value;
      panTo(canvasCamera.left ?? (first.x+CARD.width/2)*scale-view.clientWidth/2,canvasCamera.top ?? Math.max(0,first.y*scale-40));
    } else if(mode==='kanban') {kanban.scrollLeft=boardScroll.left;kanban.scrollTop=boardScroll.top;}
    // A refresh immediately after a view switch must not restore the previous view.
    persist();save();
  }
  function choose(id,focus=false) {
    if(!layout.points.has(id)) return;
    const before=byId.get(active);
    if(before) {before.button.tabIndex=-1;before.button.removeAttribute('aria-current');before.el.classList.remove('is-current');}
    active=id; interacted=true;
    const node=byId.get(id);node.el.classList.add('is-current');node.button.setAttribute('aria-current','step');node.button.tabIndex=0;
    mark(id);if(focus) node.button.focus({preventScroll:true});
  }
  function center(id) {
    const p=layout.points.get(id);
    if(!p) return;
    const h=card.height;
    panTo((p.x+CARD.width/2)*scale-view.clientWidth/2,(p.y+h/2)*scale-view.clientHeight/2);
  }
  function zoom(value,x=view.clientWidth/2,y=view.clientHeight/2) {
    const position=camera(), ratio=value/scale;
    scale=value;setSize();panTo((position.left+x)*ratio-x,(position.top+y)*ratio-y);save();
  }
  function zoomBy(factor,x,y) {zoom(bound(scale*factor,Math.min(scale,.12),3),x,y);}
  function fit() {
    if(!view.clientWidth || !view.clientHeight) return;
    zoom(Math.min(1,(view.clientWidth-8)/layout.width,(view.clientHeight-8)/layout.height));panTo((layout.width*scale-view.clientWidth)/2,(layout.height*scale-view.clientHeight)/2);save();
  }
  function toggle(id,expanded) {
    if(!graph.byId.get(id)?.children.length || !layout.points.has(id)) return;
    choose(id);
    const old=layout.points.get(id);
    if(expanded ?? folds.has(id)) folds.delete(id);else folds.add(id);
    draw();
    const next=layout.points.get(id);
    if(next) view.scrollBy((next.x-old.x)*scale,(next.y-old.y)*scale);
    byId.get(id).button.focus({preventScroll:true});save();
  }
  function resetCard(id) {
    if(!moves.delete(id)) return;
    draw();choose(id,true);center(id);save();
  }
  function closePop(focus=false) {
    if(!pop) return;
    const current=pop;pop='';
    $(`[data-pop="${current}"]`).hidden=true;
    const trigger=$(`[data-command="${current}"]`);trigger.setAttribute('aria-expanded','false');
    if(focus) trigger.focus({preventScroll:true});
    board.dispatchEvent(new Event('mapidle',{bubbles:true}));
  }
  function showPop(name) {
    if(pop===name) {closePop(true);return;}
    closePop();pop=name;$(`[data-pop="${name}"]`).hidden=false;
    if(name==='options') $('.map-nav-option').hidden=view.clientWidth<600 || viewMode!=='canvas';
    if(name!=='keys') $(`[data-pop="${name}"]`).style.top=$('.map-tools').offsetHeight+4+'px';
    $(`[data-command="${name}"]`).setAttribute('aria-expanded','true');
  }
  $('[data-show-nav]').checked=showNav;
  $('[data-show-nav]').onchange=e=>{showNav=e.target.checked;setSize();save();};
  $('[data-all-arrows]').checked=allArrows;
  $('[data-all-arrows]').onchange=e=>{allArrows=e.target.checked;paths.forEach(updateArrow);save();};
  $('[data-all]').onchange=e=>{
    snapshot();all=e.target.checked;initMode();
    closePop();draw();center(active);save();
  };
  function command(name) {
    if(['keys','options'].includes(name)) {showPop(name);return;}
    closePop();
    if(name==='fit') fit();
    if(name==='direction') {direction=orientation()==='LR'?'TB':'LR';moves.clear();draw();center(active);}
    if(name==='arrange') {moves.clear();draw();center(active);}
    if(name==='overview' || name==='unfold') {
      const suggested=overviewFolds(graph);
      for (const n of graph.vertices) {
        if(name==='overview' && suggested.has(n.id)) folds.add(n.id);else folds.delete(n.id);
      }
      draw();center(active);
    }
    save();
  }
  board.addEventListener('click',event=>{
    const btn=event.target.closest('button');if(!btn) return;
    if(btn.dataset.planView) showView(btn.dataset.planView);
    else if(btn.hasAttribute('data-reset-card')) resetCard(btn.closest('[data-vertex]').dataset.vertex);
    else if(btn.hasAttribute('data-branch')) toggle(btn.dataset.branch);
    else if(btn.dataset.command) command(btn.dataset.command);
    else if(btn.hasAttribute('data-zoom')) Number(btn.dataset.zoom)?zoomBy(1.25**Number(btn.dataset.zoom)):zoom(1);
    else if(btn.matches('.map-node')) choose(btn.closest('[data-vertex]').dataset.vertex);
  });
  view.onpointerover=e=>{if(e.pointerType!=='touch') {const el=e.target.closest('[data-vertex]');mark(el?.dataset.vertex || (interacted?active:null));}};
  view.onpointerleave=()=>mark(interacted?active:null);
  view.addEventListener('focusin',e=>{const node=e.target.closest('[data-vertex]');if(node) choose(node.dataset.vertex);});
  view.onscroll=()=>{view.style.setProperty('--lane-inset',(camera().left/scale+12)+'px');nav.position();save();};
  kanban.onscroll=save;
  board.onfocusout=e=>{if(e.relatedTarget && !board.contains(e.relatedTarget)) closePop();};
  board.onpointerdown=e=>{if(pop && !e.target.closest('[data-pop],[data-command="'+pop+'"]')) closePop();};
  function navigate(key) {
    const n=graph.byId.get(active);if(!n)return;
    if(key==='ArrowLeft' || key==='ArrowRight') {
      const right=key==='ArrowRight';
      if(!n.toggle.hidden && right===folds.has(active)) {toggle(active,right);return;}
      const target=(right?n.children:[n.primary,...n.parents]).find(id=>layout.points.has(id));
      if(target!=null) {choose(target,true);center(target);}

    } else {
      const ids=[...layout.points.keys()],idx=ids.indexOf(active);
      const id=ids[bound(idx+(key==='ArrowDown'?1:-1),0,ids.length-1)];choose(id,true);center(id);
    }
    save();
  }
  board.addEventListener('keydown',event=>{
    if(event.key==='Escape' && pop) {event.preventDefault();event.stopPropagation();closePop(true);return;}
    if(pop || event.target.matches('input,select,textarea,[role="separator"]') || event.metaKey || event.altKey || event.ctrlKey) return;
    const key=event.key.toLowerCase();
    if(key==='r' && view.contains(event.target)) resetCard(event.target.closest('[data-vertex]')?.dataset.vertex || active);
    else if(key==='0' && viewMode==='canvas') command('fit');
    else if(view.contains(event.target) && key.startsWith('arrow')) navigate(event.key);
    else if(event.target===view && key==='enter' && active) byId.get(active).button.click();
    else return;
    event.preventDefault();
  });
  function resize(h) {
    const max=Math.max(220,innerHeight-96);
    if(h) {height=bound(Math.round(h),180,max);board.style.setProperty('--map-height',height+'px');}
    grip.setAttribute('aria-valuemin','180');grip.setAttribute('aria-valuemax',max);
    grip.setAttribute('aria-valuenow',height || view.clientHeight || 360);
  }
  function move(id,start,dx,dy) {
    const p={x:Math.max(64,start.x+dx),y:Math.max(80,start.y+dy)}, el=byId.get(id).el;
    const offset={x:start.offset.x+p.x-start.x,y:start.offset.y+p.y-start.y};
    if(Math.abs(offset.x)<.01 && Math.abs(offset.y)<.01) moves.delete(id);else moves.set(id,offset);
    layout.points.set(id,p);el.style.left=p.x+'px';el.style.top=p.y+'px';
    for(const edge of incident.get(id) || []) edge.path.setAttribute('d',edgeD(edge));
    if(p.x+card.width+24>layout.width || p.y+card.height+20>layout.height) {
      layout.width=Math.max(layout.width,p.x+card.width+24);layout.height=Math.max(layout.height,p.y+card.height+20);setSize();
    }
  }
  bindMapGestures({board,view,grip,resize,zoomBy,camera,panTo,getScale:()=>scale,save,
    startNode:id=>({...layout.points.get(id),offset:moves.get(id)||{x:0,y:0}}),
    select:id=>choose(id,true),move,endNode:(id,start)=>{
      const clear=clearDrop(layout.points,id,card);
      move(id,start,clear.x-start.x,clear.y-start.y);
      draw();save();
    }});
  initMode();active=saved.active;interacted=Boolean(active);draw();resize(height);
  function restore(s) {
    if(!s || !map.getClientRects().length) return;
    if(viewMode==='kanban') {kanban.scrollLeft=s.boardLeft || 0;kanban.scrollTop=s.boardTop || 0;return;}
    if(viewMode!=='canvas') return;
    if(s.active && layout.points.has(s.active)) choose(s.active,s.focused);
    const first=layout.points.get(active) || layout.points.values().next().value;
    panTo(s.left ?? (first.x+CARD.width/2)*scale-view.clientWidth/2,s.top ?? Math.max(0,first.y*scale-40));
  }
  let viewportWidth=view.clientWidth, viewportHeight=view.clientHeight;
  const sizeObserver=new ResizeObserver(()=>{
    if(!view.clientWidth) return;
    const previous=camera(), width=view.clientWidth, heightNow=view.clientHeight;
    const changed=viewportWidth && (width!==viewportWidth || heightNow!==viewportHeight);
    if(height) resize(height);
    setSize();
    if(changed) panTo(previous.left+(viewportWidth-width)/2,previous.top+(viewportHeight-heightNow)/2);
    viewportWidth=width;viewportHeight=heightNow;
  });
  sizeObserver.observe(view);
  // Apply after initial layout so hidden canvas geometry stays available on return.
  const initialMode=viewMode;viewMode='kanban';showView(initialMode);
  restore(saved);
  return {snapshot,restore,dispose:()=>{board.dispatchEvent(new Event('mapdispose'));sizeObserver.disconnect();cancelAnimationFrame(raf);},suspend:()=>closePop()};
}

export function mountMaps(panel, previous = new Map()) {
  for(const map of panel.querySelectorAll('.plan-map')) {
    const saved=previous.get(map.dataset.map);
    if(mounted.has(map)) mounted.get(map).restore(saved);
    else mounted.set(map,mount(map,saved));
  }
}
export function captureMaps(panel, {suspend=false,dispose=false}={}) {
  const views=new Map();
  for(const map of panel.querySelectorAll('.plan-map')) {
    const controller=mounted.get(map);
    if(controller) {views.set(map.dataset.map,controller.snapshot());if(suspend) controller.suspend();if(dispose) controller.dispose();}
  }
  return views;
}
