import {escapeHtml as esc} from './core.mjs';

// Non-scheduler operations stay inspectable, while edges connect their nearest
// upstream calculations through any intervening preparation or analysis nodes.
export function planMap(layers, key, attribute) {
  const parents = new Map(), positions = new Map(), auxiliary = [];
  const cards = [], edges = [];
  let column = 0, rows = 1;
  function button(node) {
    return `<button type="button" class="map-node" ${attribute}="${esc(node.id)}" data-state="${esc(node.state)}" title="${esc(node.description || node.title)}"><strong>${esc(node.title)}</strong><small>${esc(node.status)}${attribute==='data-open-step'?'<span data-ionic-step></span>':''}</small></button>`;
  }
  for (const layer of layers) {
    let row = 0;
    for (const node of layer) {
      const upstream = new Set(node.dependencies.flatMap(id => parents.get(id) || []));
      if (node.auxiliary) {
        auxiliary.push(button(node));
        parents.set(node.id, [...upstream]);
        continue;
      }
      const x = 14 + column * 194, y = 18 + row++ * 92;
      positions.set(node.id, {x, y});
      parents.set(node.id,[node.id]);
      cards.push(`<div class="map-position" data-vertex="${esc(node.id)}" data-column="${column}" data-parents="${esc(JSON.stringify([...upstream]))}" style="left:${x}px;top:${y}px">${button(node)}</div>`);
      for (const id of upstream) {
        const start = positions.get(id);
        const mid = (start.x + 152 + x) / 2;
        edges.push(`<path data-source="${esc(id)}" data-target="${esc(node.id)}" d="M${start.x+152} ${start.y+32} C${mid} ${start.y+32},${mid} ${y+32},${x} ${y+32}"/>`);
      }
    }
    rows = Math.max(rows,row);
    if (row) column++;
  }
  const width = Math.max(column*194,180), height = rows*92+18;
  const graph = !cards.length ? '' : `<div class="map-tools"><button type="button" data-spatial aria-pressed="false">3D</button><button type="button" data-zoom="-1" aria-label="缩小方案">−</button><button type="button" data-zoom="0" aria-label="还原缩放">100%</button><button type="button" data-zoom="1" aria-label="放大方案">＋</button></div><div class="map-camera" hidden><label>水平旋转<input type="range" data-angle="yaw" min="-35" max="35" step="1" aria-label="水平旋转"></label><label>俯仰<input type="range" data-angle="pitch" min="-20" max="20" step="1" aria-label="俯仰"></label></div><div class="map-window" tabindex="0" aria-label="计算依赖图"><div class="map-size"><div class="map-world" style="width:${width}px;height:${height}px"><svg width="${width}" height="${height}" aria-hidden="true">${edges.join('')}</svg>${cards.join('')}</div></div></div>`;
  return `<div class="plan-map" data-map="${esc(key)}">${graph}${auxiliary.length ? `<details data-fold="auxiliary"><summary>辅助操作 · ${auxiliary.length}</summary><div class="map-aux">${auxiliary.join('')}</div></details>` : ''}</div>`;
}

// The input order is topological. A visible join needs only one open upstream path.
export function reachableVertices(vertices, folded) {
  const visible = new Set();
  for (const {id, parents} of vertices) {
    if (!parents.length || parents.some(parent => visible.has(parent) && !folded.has(parent))) visible.add(id);
  }
  return visible;
}

// Orthographic yaw then pitch. Labels face the camera so text remains legible.
export function projectPoint(x, y, z, yaw, pitch) {
  const azimuth = yaw * Math.PI / 180, elevation = pitch * Math.PI / 180;
  const depth = z * Math.cos(azimuth) - x * Math.sin(azimuth);
  return {
    x: x * Math.cos(azimuth) + z * Math.sin(azimuth),
    y: y * Math.cos(elevation) - depth * Math.sin(elevation),
    depth: y * Math.sin(elevation) + depth * Math.cos(elevation)
  };
}

const snapshots = new WeakMap();

function storedView(key) {
  try { return JSON.parse(sessionStorage.getItem(key)); } catch { return null; }
}

export function mountMaps(panel, previous = new Map()) {
  for (const map of panel.querySelectorAll('.plan-map')) {
    const view = map.querySelector('.map-window');
    if (!view) continue;
    const world = view.querySelector('.map-world'), size = world.parentElement;
    const storageKey = 'chemistry.planView.' + map.dataset.map;
    const saved = previous.get(map.dataset.map) || storedView(storageKey);
    const vertices = [...world.querySelectorAll('[data-vertex]')].map(el => ({
      el, id:el.dataset.vertex, column:Number(el.dataset.column), parents:JSON.parse(el.dataset.parents),
    }));
    const branches = new Set(vertices.flatMap(node => node.parents));
    const folded = new Set((Array.isArray(saved?.folded) ? saved.folded : []).filter(id => branches.has(id)));
    let spatial = saved?.spatial === true;
    const angles = {yaw: Number(saved?.yaw ?? 20), pitch:Number(saved?.pitch ?? -10)};
    const paths = [...world.querySelectorAll('path[data-source]')];
    const camera = map.querySelector('.map-camera');
    const mode = map.querySelector('[data-spatial]');
    function snapshot() {
      return {left:view.scrollLeft, top:view.scrollTop, scale:Number(map.dataset.scale),
        folded:[...folded], spatial, ...angles};
    }
    snapshots.set(map,snapshot);
    function save() {
      if (!view.getClientRects().length) return;
      try { sessionStorage.setItem(storageKey,JSON.stringify(snapshot())); } catch { /* Optional. */ }
    }

    function layout() {
      const visible = reachableVertices(vertices,folded);
      const points = new Map(), rows = new Map();
      let minX=Infinity, minY=Infinity, maxX=-Infinity, maxY=-Infinity;
      for (const vertex of vertices) {
        vertex.el.hidden = !visible.has(vertex.id);
        if (vertex.el.hidden) continue;
        const row = rows.get(vertex.column) || 0;
        rows.set(vertex.column,row+1);
        const x=vertex.column*210, y=row*108;
        const z=46*Math.log2(1+vertex.column)+38*(row%4);
        const point=spatial ? projectPoint(x,y,z,angles.yaw,angles.pitch) : {x,y,depth:0};
        points.set(vertex.id,point);
        minX=Math.min(minX,point.x); minY=Math.min(minY,point.y);
        maxX=Math.max(maxX,point.x); maxY=Math.max(maxY,point.y);
      }
      const width=Math.ceil(maxX-minX+184), height=Math.ceil(maxY-minY+118);
      world.style.width=width+'px'; world.style.height=height+'px';
      const svg=world.querySelector('svg');
      svg.setAttribute('width',width); svg.setAttribute('height',height);
      for (const {id,el} of vertices) {
        const p=points.get(id);
        if (!p) continue;
        p.x+=16-minX; p.y+=12-minY;
        el.style.left=p.x+'px'; el.style.top=p.y+'px';
        el.style.zIndex=String(Math.max(1,100000+Math.round(p.depth)));
        const toggle=el.querySelector('[data-branch]');
        if (toggle) {
          toggle.setAttribute('aria-expanded',String(!folded.has(id)));
          toggle.textContent=folded.has(id)?'＋ 展开分支':'− 收起分支';
        }
      }
      for (const path of paths) {
        const {source,target}=path.dataset;
        const start=points.get(source), end=points.get(target);
        const shown=start && end && !folded.has(source);
        path.style.display=shown?'':'none';
        if (!shown) continue;
        const x1=start.x+152, y1=start.y+32, x2=end.x, y2=end.y+32, mid=(x1+x2)/2;
        path.setAttribute('d',`M${x1} ${y1} C${mid} ${y1},${mid} ${y2},${x2} ${y2}`);
      }
      camera.hidden=!spatial;
      mode.setAttribute('aria-pressed',String(spatial));
      zoom(Number(map.dataset.scale || 1));
    }
    for (const {el,id} of vertices) {
      if (!branches.has(id)) continue;
      let toggle=el.querySelector('[data-branch]');
      if (!toggle) {
        toggle=document.createElement('button');
        toggle.type='button'; toggle.dataset.branch=id;
        el.append(toggle);
      }
      toggle.onclick=()=>{
        const before=el.getBoundingClientRect();
        if (folded.has(id)) folded.delete(id); else folded.add(id);
        layout();
        const after=el.getBoundingClientRect();
        view.scrollBy(after.left-before.left,after.top-before.top);
        save();
      };
    }
    mode.onclick=()=>{spatial=!spatial;layout();save();};
    for (const input of camera.querySelectorAll('input')) {
      input.value=angles[input.dataset.angle];
      input.oninput=()=>{angles[input.dataset.angle]=Number(input.value);layout();save();};
    }
    view.onscroll=save;
    function zoom(factor, cx = view.clientWidth/2, cy = view.clientHeight/2) {
      const old = Number(map.dataset.scale || 1);
      const scale = Math.max(.4,Math.min(2.5,factor));
      const left = (view.scrollLeft+cx)*scale/old-cx, top=(view.scrollTop+cy)*scale/old-cy;
      world.style.transform = `scale(${scale})`;
      size.style.width = parseFloat(world.style.width)*scale+'px';
      size.style.height = parseFloat(world.style.height)*scale+'px';
      map.dataset.scale = scale;
      map.querySelector('[data-zoom="0"]').textContent=Math.round(scale*100)+'%';
      view.scrollTo(left,top);
    }
    layout();
    zoom(saved?.scale || 1);
    if (saved) view.scrollTo(saved.left,saved.top);
    map.querySelector('.map-tools').onclick = event => {
      const control = event.target.closest('[data-zoom]');
      if (control) {
        zoom(Number(control.dataset.zoom) ? Number(map.dataset.scale)*1.25**Number(control.dataset.zoom) : 1);
        save();
      }
    };
    view.onwheel = event => {
      if (!event.ctrlKey && !event.metaKey) return;
      event.preventDefault();
      const rect=view.getBoundingClientRect();
      zoom(Number(map.dataset.scale)*Math.exp(-event.deltaY*.008),event.clientX-rect.left,event.clientY-rect.top);
      save();
    };
    let drag;
    view.onpointerdown = event => {
      if (event.button || event.target.closest('button') || event.pointerType==='touch') return;
      drag=[event.clientX,event.clientY,view.scrollLeft,view.scrollTop];
      view.setPointerCapture(event.pointerId);
    };
    view.onpointermove = event => { if(drag) view.scrollTo(drag[2]+drag[0]-event.clientX,drag[3]+drag[1]-event.clientY); };
    view.onlostpointercapture = () => { drag=null; };
  }
}

export function captureMaps(panel) {
  return new Map([...panel.querySelectorAll('.map-window')].map(view => {
    const map=view.closest('.plan-map');
    return [map.dataset.map,snapshots.get(map)?.()];
  }));
}
