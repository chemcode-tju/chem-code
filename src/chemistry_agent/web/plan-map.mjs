import {escapeHtml as esc} from './core.mjs';
import {CARD, FOLDER_HEIGHT, buildGraph, clearDrop, edgePath, layoutGraph, overviewFolds, taskLabel} from './plan-layout.mjs';
import {inSection} from './plan-folders.mjs';
import {bindMapGestures} from './map-gestures.mjs';
import {createNavigator} from './map-navigator.mjs';
import {getLanguage, t} from './i18n.mjs';

const icon = id => `<svg aria-hidden="true"><use href="/assets/ui-icons.svg#i-${id}"/></svg>`;
const tool = (attr,label,content) => `<button type="button" ${attr} aria-label="${label}" title="${label}">${content}</button>`;
const shortcut = (keys,label) => `<span><kbd>${keys}</kbd> <span>${label}</span></span>`;
const serial = value => esc(JSON.stringify(value));

export function planMap(layers, key, attribute, title = '') {
  const nodes = layers.flat();
  if (!nodes.length) return '';
  const cards = nodes.map(n => {
    const label=taskLabel(n), split=n.title===n.id && label!==n.id ? label.lastIndexOf(' · ') : -1;
    return `<div class="map-position" data-vertex="${esc(n.id)}" data-label="${esc(label)}" data-group-path="${serial(n.groupPath ?? null)}" data-parents="${serial(n.dependencies)}" ${n.auxiliary?'data-aux':''} hidden>
    <button type="button" class="map-node" ${attribute}="${esc(n.id)}" data-state="${esc(n.state)}" title="${esc([...new Set([label,n.id,n.description].filter(Boolean))].join(' · '))}" tabindex="-1">
      ${n.groupPath?.length?`<span class="map-card-context">${esc(n.groupPath.join(' › '))}</span>`:split<0?'':`<span class="map-card-context">${esc(label.slice(0,split))}</span>`}<strong>${esc(split<0?label:label.slice(split+3))}</strong><small><i aria-hidden="true"></i>${esc(n.status)}${attribute==='data-open-step'?'<span data-ionic-step></span>':''}</small>
    </button><button type="button" data-reset-card aria-label="恢复这张卡片原位" title="恢复这张卡片原位" hidden>${icon('reset')}<span>归位</span></button><button type="button" data-branch="${esc(n.id)}" tabindex="-1" hidden></button></div>`;
  }).join('');
  return `<div class="plan-map" data-map="${esc(key)}"><div class="map-board" role="region" aria-label="方案画布">
    <div class="map-tools"><div class="map-identity">${icon('map')}<strong>方案画布</strong><span data-map-count title="可见节点 / 总节点"></span></div><div class="map-actions">
      ${tool('data-command="find" aria-expanded="false"','查找步骤',icon('find'))}
      ${tool('data-command="options" aria-expanded="false"','画布选项',icon('more'))}
      ${tool('data-command="full" aria-pressed="false"','全屏画布',icon('expand'))}
    </div></div>
    <div class="map-subtools"><select data-groups aria-label="按分支查看"></select>
      <div class="map-zoom">${tool('data-zoom="-1"','缩小方案',icon('minus'))}${tool('data-zoom="0"','还原缩放','100%')}${tool('data-zoom="1"','放大方案',icon('plus'))}${tool('data-command="fit"','适应画布',icon('fit'))}</div></div>
    <div class="map-pop map-search" data-pop="find" hidden><input type="search" placeholder="查找名称或步骤 ID" aria-label="查找名称或步骤 ID" autocomplete="off"><div data-results></div></div>
    <div class="map-pop" data-pop="options" hidden>
      <label data-aux-option><input type="checkbox" data-all> 显示建模与分析步骤</label>
      <button type="button" data-command="overview">分支概览</button><button type="button" data-command="unfold">展开全部节点</button>
      <button type="button" data-command="arrange">恢复自动布局</button><label class="map-nav-option"><input type="checkbox" data-show-nav> 全屏缩略导航</label></div>
    <div class="map-window" tabindex="0" aria-label="计算依赖图"><div class="map-size"><div class="map-world"><div class="map-lanes"></div><svg class="map-edges" aria-hidden="true"></svg>${cards}</div></div></div>
    <button type="button" class="map-navigator" title="点击或拖动缩略图定位" aria-label="缩略图导航"><svg preserveAspectRatio="none" aria-hidden="true"><path/><rect/></svg></button>
    <div class="map-foot"><button type="button" data-command="keys" aria-expanded="false" title="画布快捷键">${icon('keys')}<span>快捷键</span></button><span class="map-quick">← 收起 · → 展开</span><span class="map-paper-title">${esc(title)}</span></div>
    <div class="map-pop map-help" data-pop="keys" hidden>
      ${shortcut('← / →','收起 / 展开分支')}${shortcut('↑ / ↓','上一个 / 下一个节点')}${shortcut('Enter','查看步骤详情')}${shortcut('/','查找步骤')}${shortcut('R','恢复这张卡片原位')}${shortcut('F','切换全屏')}${shortcut('0','适应画布')}${shortcut('Esc','关闭浮层或全屏')}${shortcut('↔','拖动卡片移动，拖动空白平移')}
    </div><div class="map-grip" role="separator" tabindex="0" aria-orientation="horizontal" aria-label="调整画布高度" title="拖动调整画布高度"><i></i></div>
    </div><dialog class="map-dialog" aria-label="全屏方案画布"></dialog></div>`;
}

const mounted = new WeakMap();
const bound = (v,min,max) => Math.max(min,Math.min(max,v));
function stored(key) { try {return JSON.parse(sessionStorage.getItem(key));} catch {return null;} }

function mount(map, previous) {
  const $ = selector=>map.querySelector(selector);
  const storageKey='chemistry.planView.'+map.dataset.map;
  const saved=previous || stored(storageKey) || {};
  const board=$('.map-board'), view=$('.map-window'), world=$('.map-world'), size=$('.map-size'), svg=$('.map-edges');
  const dialog=$('.map-dialog'), select=$('[data-groups]'), find=$('.map-search input'), results=$('[data-results]'), grip=$('.map-grip');
  world.style.setProperty('--node-w',CARD.width+'px');world.style.setProperty('--node-h',CARD.height+'px');
  const nodes=[...world.querySelectorAll('[data-vertex]')].map(el=>({id:el.dataset.vertex,title:el.dataset.label,
    parents:JSON.parse(el.dataset.parents),groupPath:JSON.parse(el.dataset.groupPath),reset:el.querySelector('[data-reset-card]'),auxiliary:el.hasAttribute('data-aux'),el,button:el.querySelector('.map-node'),toggle:el.querySelector('[data-branch]')}));
  const byId=new Map(nodes.map(n=>[n.id,n])), onlyAux=nodes.every(n=>n.auxiliary);
  const graphs=new Map(), modes={...saved.modes};
  let all=saved.all===true || onlyAux, graph, folds, closed, moves, group, layout, active=null, lit=null, paths=[], incident;
  let showNav=saved.showNav!==false;
  const nav=createNavigator($('.map-navigator'),view,()=>({layout,scale,...CARD}));
  let scale=bound(Number(saved.scale)||1,.005,3), height=saved.height || 0, pop='', raf=0, interacted=false;
  const cnEn=(cn,en)=>getLanguage()==='en'?en:cn;
  const folderButtons=new Map();
  const folderTitle=folder=>folder.batch ? t('并行分支')+' '+folder.title : folder.title;
  function initMode() {
    if (!graphs.has(all)) graphs.set(all,buildGraph(nodes,all));
    graph=graphs.get(all);
    const state=modes[all] || {};
    folds=new Set((state.folds || (graph.vertices.length>32 ? [...overviewFolds(graph)] : [])).filter(id=>graph.byId.has(id)));
    moves=new Map((state.moves || []).filter(([id,p])=>graph.byId.has(id) && Number.isFinite(p?.x) && Number.isFinite(p?.y)));
    const opened=new Set(state.openFolders || []);
    closed=new Set([...graph.folders.keys()].filter(id=>state.openFolders ? !opened.has(id) : graph.vertices.length>32));
    if (!state.openFolders && graph.folders.size) {
      // An older saved overview may hide every new folder behind its shared clean surface.
      for (const id of folds) {
        if (graph.byId.get(id).children.some(child=>graph.byId.get(child).folderIds.length)) folds.delete(id);
      }
    }
    group=graph.filters.some(g=>g.id===state.group)?state.group:null;
    $('[data-all]').checked=all; $('[data-aux-option]').hidden=onlyAux || nodes.every(n=>!n.auxiliary);
    select.innerHTML='<option value="">'+t('全部分支')+'</option>'+graph.filters.map(g=>{
      const label=g.folder && !g.batch ? g.path.join(' › ') : g.folder ? [g.origin,folderTitle(g)].filter(Boolean).join(' › ') : g.title;
      return `<option value="${esc(g.id)}">${esc(label)} · ${g.members.length}</option>`;
    }).join('');
    select.value=group || '';select.disabled=graph.filters.length<2;
  }
  function snapshot() {
    modes[all]={folds:[...folds],moves:[...moves],group,openFolders:[...graph.folders.keys()].filter(id=>!closed.has(id))};
    const focused=board.contains(document.activeElement);
    const focusedFolder=focused ? document.activeElement.closest('[data-folder]')?.dataset.folder : null;
    return {modes,all,active,scale,height,showNav,left:view.scrollLeft,top:view.scrollTop,full:dialog.open,focused,focusedFolder};
  }
  function save() {
    if (!board.getClientRects().length) return;
    cancelAnimationFrame(raf);
    raf=requestAnimationFrame(()=>{if(!map.isConnected || !view.clientWidth) return;
      try {const {full,focused,focusedFolder,...persist}=snapshot();sessionStorage.setItem(storageKey,JSON.stringify(persist));} catch { /* Optional. */ }
    });
  }
  function setSize() {
    world.style.width=layout.width+'px'; world.style.height=layout.height+'px';
    size.style.marginTop=(dialog.open?Math.max(0,(view.clientHeight-layout.height*scale)/2):0)+'px';
    size.style.width=Math.ceil(layout.width*scale)+'px'; size.style.height=Math.ceil(layout.height*scale)+'px';
    world.style.transform=`scale(${scale})`;map.dataset.scale=scale;
    $('[data-zoom="0"]').textContent=Math.round(scale*100)+'%';
    view.style.setProperty('--lane-inset',(view.scrollLeft/scale+12)+'px');
    $('.map-navigator').hidden=!showNav || !dialog.open || (layout.width*scale<=view.clientWidth && layout.height*scale<=view.clientHeight);
    nav.position();
  }
  function mark(id) {
    if (id===lit) return;
    function apply(key,on) {
      if(!key) return;
      for(const edge of incident?.get(key) || []) {
        edge.path.classList.toggle('is-traced',on);
        const other=edge.source===key ? edge.target : edge.source;
        (byId.get(other)?.el || folderButtons.get(other))?.classList.toggle('is-neighbor',on);
      }
    }
    apply(lit,false); lit=id; apply(id,true);
  }
  let compact=view.clientWidth>0 && view.clientWidth<530;
  function endpoint(id) {
    if(layout.points.has(id)) return layout.points.get(id);
    const header=layout.headers.get(id);
    return {x:header.x,y:header.y+(FOLDER_HEIGHT-CARD.height)/2};
  }
  function edgeD(edge) {
    const rail=compact && (edge.folder || graph.byId.get(edge.source).section!==graph.byId.get(edge.target).section);
    return edgePath(endpoint(edge.source),endpoint(edge.target),rail);
  }
  function folderMarkup({x,y,section,count}) {
    const expanded=!closed.has(section.id);
    return `<button type="button" class="map-folder" data-folder="${esc(section.id)}" style="left:${x}px;top:${y}px;height:${FOLDER_HEIGHT}px"
      aria-expanded="${expanded}" title="${esc([section.origin,...section.path].filter(Boolean).join(' › '))}"><i aria-hidden="true"></i><strong>${esc(folderTitle(section))}</strong><small>${count} ${t(all?'步':'项计算')}</small></button>`;
  }
  function draw() {
    mark(null); layout=layoutGraph(graph,folds,moves,group,compact,closed);
    const first=layout.points.keys().next().value;
    if (!layout.points.has(active)) active=first;
    const bands=world.querySelector('.map-lanes');
    bands.innerHTML=layout.lanes.map(({group:g,start,height:h,count},i)=>`<div class="map-lane" style="top:${start}px;height:${h}px"><span><b>${String(i+1).padStart(2,'0')}</b>${esc(g.title)} <small>${count}/${g.members.length}</small></span></div>`).join('')
      +[...layout.headers.values()].map(folderMarkup).join('');
    folderButtons.clear();
    for(const button of bands.querySelectorAll('[data-folder]')) folderButtons.set(button.dataset.folder,button);
    paths=[];incident=new Map();
    for (const n of nodes) {
      const p=layout.points.get(n.id);
      n.el.hidden=!p; if (!p) continue;
      n.reset.hidden=!moves.has(n.id);
      n.el.style.left=p.x+'px';n.el.style.top=p.y+'px';n.button.tabIndex=n.id===active?0:-1;
      n.el.classList.toggle('is-current',interacted && n.id===active);
      if(interacted && n.id===active) n.button.setAttribute('aria-current','step');else n.button.removeAttribute('aria-current');
      const vertex=graph.byId.get(n.id), children=vertex.children.filter(id=>inSection(graph.byId.get(id),group));
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
    for(const link of layout.links) paths.push(link);
    svg.innerHTML=paths.map(e=>`<path data-source="${esc(e.source)}" data-target="${esc(e.target)}" ${e.folder?'class="folder-edge"':e.secondary?'class="secondary-edge"':''} d="${edgeD(e)}"/>`).join('');
    for (const [i,path] of [...svg.children].entries()) {
      const e=paths[i];e.path=path;
      for (const id of [e.source,e.target]) {if(!incident.has(id)) incident.set(id,[]);incident.get(id).push(e);}
    }
    const tally=layout.points.size+' / '+graph.vertices.length;
    $('[data-map-count]').textContent=layout.headers.size ? tally+' · '+layout.headers.size+cnEn(' 组',' groups') : tally;
    setSize();nav.draw();if(interacted) mark(active);
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
    const p=layout.points.get(id) || layout.headers.get(id);
    if(!p) return;
    const h=layout.headers.has(id)?FOLDER_HEIGHT:CARD.height;
    view.scrollTo((p.x+CARD.width/2)*scale-view.clientWidth/2,(p.y+h/2)*scale-view.clientHeight/2);
  }
  function zoom(value,x=view.clientWidth/2,y=view.clientHeight/2) {
    const inset=s=>Math.max(0,(view.clientWidth-layout.width*s)/2);
    const insetY=s=>dialog.open?Math.max(0,(view.clientHeight-layout.height*s)/2):0;
    const ratio=value/scale,left=(view.scrollLeft+x-inset(scale))*ratio+inset(value)-x;
    const top=(view.scrollTop+y-insetY(scale))*ratio+insetY(value)-y;
    scale=value;setSize();view.scrollTo(left,top);save();
  }
  function zoomBy(factor,x,y) {zoom(bound(scale*factor,Math.min(scale,.12),3),x,y);}
  function fit() {
    if(!view.clientWidth || !view.clientHeight) return;
    zoom(Math.min(1,(view.clientWidth-8)/layout.width,(view.clientHeight-8)/layout.height));view.scrollTo(0,0);save();
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
  function toggleFolder(id,expand=closed.has(id)) {
    const header=layout.headers.get(id);
    if(!header) return;
    if(expand) closed.delete(id);else closed.add(id);
    draw();
    const next=layout.headers.get(id);
    if(next) view.scrollBy((next.x-header.x)*scale,(next.y-header.y)*scale);
    folderButtons.get(id)?.focus({preventScroll:true});save();
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
    $(`[data-command="${name}"]`).setAttribute('aria-expanded','true');
    if(name==='find'){find.value='';search();find.focus();}
  }
  function setFull(on,focus=true) {
    if(on===dialog.open) return;
    closePop();
    const left=view.scrollLeft,top=view.scrollTop;
    if(on) {dialog.append(board);dialog.showModal();}
    else {dialog.close();map.insertBefore(board,dialog);}
    const trigger=$('[data-command="full"]'),label=t(on?'退出全屏':'全屏画布');
    trigger.setAttribute('aria-pressed',String(on));trigger.title=label;trigger.setAttribute('aria-label',label);
    trigger.innerHTML=icon(on?'shrink':'expand');
    if(focus) trigger.focus({preventScroll:true});
    const narrow=view.clientWidth>0 && view.clientWidth<530;
    if(compact!==narrow) {compact=narrow;draw();} else setSize();
    view.scrollTo(left,top);
  }
  dialog.addEventListener('cancel',e=>{e.preventDefault();if(pop) closePop(true);else setFull(false);});
  function search() {
    const query=find.value.trim().toLowerCase();
    const hits=nodes.filter(n=>query?(n.id+' '+n.title+' '+(n.groupPath || []).join(' ')).toLowerCase().includes(query):layout.points.has(n.id));
    const rows=hits.slice(0,30);
    results.innerHTML=rows.map(n=>`<button type="button" data-hit="${esc(n.id)}"><strong>${esc(n.title)}</strong><small>${esc(n.id)}</small></button>`).join('') ||
      '<p>'+t('没有匹配的步骤')+'</p>';
    if(hits.length>rows.length) results.insertAdjacentHTML('beforeend','<p>'+cnEn('仅显示前 30 项，请继续输入','First 30 matches; keep typing to narrow the list')+'</p>');
  }
  function jump(id) {
    if(!byId.has(id)) return;
    if(!graph.byId.has(id)) {snapshot();all=true;initMode();}
    const node=graph.byId.get(id);
    if(!inSection(node,group)) {group=null;select.value='';}
    for(const folder of node.folderIds) closed.delete(folder);
    for(let p=graph.byId.get(node.primary);p;p=graph.byId.get(p.primary)) folds.delete(p.id);
    closePop();draw();
    if(scale<.7) zoom(1);
    choose(id,true);center(id);save();
  }
  $('[data-show-nav]').checked=showNav;
  $('[data-show-nav]').onchange=e=>{showNav=e.target.checked;setSize();save();};
  find.oninput=search;
  $('[data-pop="find"]').onkeydown=e=>{
    const buttons=[...results.querySelectorAll('button')], i=buttons.indexOf(document.activeElement);
    if((e.key==='ArrowDown' || e.key==='ArrowUp') && buttons.length) {
      e.preventDefault();buttons[bound(i+(e.key==='ArrowDown'?1:-1),0,buttons.length-1)].focus();
    } else if(e.key==='Enter' && document.activeElement===find && buttons.length) {e.preventDefault();jump(buttons[0].dataset.hit);}
  };
  select.onchange=()=>{
    group=select.value || null;
    if(group) {
      for(const id of graph.filters.find(g=>g.id===group).members) folds.delete(id);
      if(graph.folders.has(group)) closed.delete(group);
    }
    draw();
    if(scale<.7) zoom(1);
    const first=layout.points.keys().next().value;
    if(folderButtons.has(group)) {folderButtons.get(group).focus({preventScroll:true});center(group);}
    else {choose(first,true);center(first);}
    save();
  };
  $('[data-all]').onchange=e=>{
    const previousFolder=graph.folders.has(group) ? group : null;
    const previousFolds=new Map([...graph.folders.keys()].map(id=>[id,closed.has(id)]));
    snapshot();all=e.target.checked;initMode();
    for(const [id,wasClosed] of previousFolds) {
      if (!graph.folders.has(id)) continue;
      if(wasClosed) closed.add(id);else closed.delete(id);
    }
    if(previousFolder && graph.folders.has(previousFolder)) {group=previousFolder;select.value=group;}
    closePop();draw();center(previousFolder || active);save();
  };
  function command(name) {
    if(['find','keys','options'].includes(name)) {showPop(name);return;}
    closePop();
    if(name==='full') setFull(!dialog.open);
    if(name==='fit') fit();
    if(name==='arrange') {moves.clear();draw();center(active);}
    if(name==='overview' || name==='unfold') {
      const suggested=overviewFolds(graph);
      for(const folder of graph.folders.values()) {
        const representative=graph.byId.get(folder.members[0]);
        if(!inSection(representative,group)) continue;
        if(name==='overview' && group!==folder.id) closed.add(folder.id);else closed.delete(folder.id);
      }
      for (const n of graph.vertices) if(inSection(n,group)) {
        if(name==='overview' && suggested.has(n.id)) folds.add(n.id);else folds.delete(n.id);
      }
      draw();center(active);
    }
    save();
  }
  board.addEventListener('click',event=>{
    const btn=event.target.closest('button');if(!btn) return;
    if(btn.hasAttribute('data-reset-card')) resetCard(btn.closest('[data-vertex]').dataset.vertex);
    else if(btn.dataset.folder) toggleFolder(btn.dataset.folder);
    else if(btn.hasAttribute('data-branch')) toggle(btn.dataset.branch);
    else if(btn.dataset.hit) jump(btn.dataset.hit);
    else if(btn.dataset.command) command(btn.dataset.command);
    else if(btn.hasAttribute('data-zoom')) Number(btn.dataset.zoom)?zoomBy(1.25**Number(btn.dataset.zoom)):zoom(1);
    else if(btn.matches('.map-node')) choose(btn.closest('[data-vertex]').dataset.vertex);
  });
  view.onpointerover=e=>{if(e.pointerType!=='touch') {const el=e.target.closest('[data-vertex],[data-folder]');mark(el?.dataset.vertex || el?.dataset.folder || (interacted?active:null));}};
  view.onpointerleave=()=>mark(interacted?active:null);
  view.onscroll=()=>{view.style.setProperty('--lane-inset',(view.scrollLeft/scale+12)+'px');nav.position();save();};
  board.onfocusout=e=>{if(e.relatedTarget && !board.contains(e.relatedTarget)) closePop();};
  board.onpointerdown=e=>{if(pop && !e.target.closest('[data-pop],[data-command="'+pop+'"]')) closePop();};
  function navigate(key) {
    const n=graph.byId.get(active);if(!n)return;
    if(key==='ArrowLeft' || key==='ArrowRight') {
      const right=key==='ArrowRight';
      if(!n.toggle.hidden && right===folds.has(active)) {toggle(active,right);return;}
      const target=(right?n.children:[n.primary,...n.parents]).find(id=>layout.points.has(id));
      if(target!=null) {choose(target,true);center(target);}
      else if(right) {
        const child=n.children.map(id=>graph.byId.get(id)).find(row=>row.folderIds.some(id=>folderButtons.has(id)));
        const folder=child?.folderIds.find(id=>folderButtons.has(id));
        if(folder) {folderButtons.get(folder).focus({preventScroll:true});center(folder);}
      }
    } else {
      const ids=[...layout.points.keys()],idx=ids.indexOf(active);
      const id=ids[bound(idx+(key==='ArrowDown'?1:-1),0,ids.length-1)];choose(id,true);center(id);
    }
    save();
  }
  board.addEventListener('keydown',event=>{
    if(event.key==='Escape' && (pop || dialog.open)) {event.preventDefault();event.stopPropagation();if(pop) closePop(true);else setFull(false);return;}
    if(pop || event.target.matches('input,select,textarea,[role="separator"]') || event.metaKey || event.altKey || event.ctrlKey) return;
    const key=event.key.toLowerCase();
    const folder=event.target.closest('[data-folder]')?.dataset.folder;
    if(folder && ['arrowleft','arrowright'].includes(key)) toggleFolder(folder,key==='arrowright');
    else if(folder && ['arrowup','arrowdown'].includes(key)) {
      const ids=[...folderButtons.keys()], next=ids[bound(ids.indexOf(folder)+(key==='arrowup'?-1:1),0,ids.length-1)];
      folderButtons.get(next).focus({preventScroll:true});center(next);
    }
    else if(key==='r' && view.contains(event.target) && !folder) resetCard(event.target.closest('[data-vertex]')?.dataset.vertex || active);
    else if(key==='/') command('find');else if(key==='f') command('full');else if(key==='0') command('fit');
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
    const p={x:Math.max(12,start.x+dx),y:Math.max(10,start.y+dy)}, el=byId.get(id).el;
    const offset={x:start.offset.x+p.x-start.x,y:start.offset.y+p.y-start.y};
    if(Math.abs(offset.x)<.01 && Math.abs(offset.y)<.01) moves.delete(id);else moves.set(id,offset);
    layout.points.set(id,p);el.style.left=p.x+'px';el.style.top=p.y+'px';
    for(const edge of incident.get(id) || []) edge.path.setAttribute('d',edgeD(edge));
    if(p.x+CARD.width+24>layout.width || p.y+CARD.height+20>layout.height) {
      layout.width=Math.max(layout.width,p.x+CARD.width+24);layout.height=Math.max(layout.height,p.y+CARD.height+20);setSize();
    }
  }
  bindMapGestures({board,view,grip,resize,zoomBy,getScale:()=>scale,save,
    startNode:id=>({...layout.points.get(id),offset:moves.get(id)||{x:0,y:0}}),
    select:id=>choose(id,true),move,endNode:(id,start)=>{
      const clear=clearDrop(layout.points,id);
      move(id,start,clear.x-start.x,clear.y-start.y);
      draw();save();
    }});
  initMode();active=saved.active;interacted=Boolean(active);draw();resize(height);
  function restore(s) {
    if(!s || !map.getClientRects().length) return;
    if(s.full) setFull(true,false);
    if(s.active && layout.points.has(s.active)) choose(s.active,s.focused && !s.focusedFolder);
    if(s.focused && s.focusedFolder) folderButtons.get(s.focusedFolder)?.focus({preventScroll:true});
    view.scrollTo(s.left || 0,s.top || 0);
  }
  const sizeObserver=new ResizeObserver(()=>{
    if(!view.clientWidth) return;
    const narrow=view.clientWidth<530;
    if(compact!==narrow) {
      compact=narrow;draw();
      if(interacted) center(active);
    }
    if(height && !dialog.open) resize(height);
    setSize();
  });
  sizeObserver.observe(view);
  restore(saved);
  return {snapshot,restore,dispose:()=>{sizeObserver.disconnect();cancelAnimationFrame(raf);},suspend:()=>{if(dialog.open)setFull(false,false);}};
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
