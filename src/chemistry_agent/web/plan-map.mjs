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
      cards.push(`<div class="map-position" style="left:${x}px;top:${y}px">${button(node)}</div>`);
      for (const id of upstream) {
        const start = positions.get(id);
        const mid = (start.x + 152 + x) / 2;
        edges.push(`<path d="M${start.x+152} ${start.y+32} C${mid} ${start.y+32},${mid} ${y+32},${x} ${y+32}"/>`);
      }
    }
    rows = Math.max(rows,row);
    if (row) column++;
  }
  const width = Math.max(column*194,180), height = rows*92+18;
  const graph = !cards.length ? '' : `<div class="map-tools"><button type="button" data-zoom="-1" aria-label="缩小方案">−</button><button type="button" data-zoom="0" aria-label="还原缩放">100%</button><button type="button" data-zoom="1" aria-label="放大方案">＋</button></div><div class="map-window" tabindex="0" aria-label="计算依赖图"><div class="map-size"><div class="map-world" style="width:${width}px;height:${height}px"><svg width="${width}" height="${height}" aria-hidden="true">${edges.join('')}</svg>${cards.join('')}</div></div></div>`;
  return `<div class="plan-map" data-map="${esc(key)}">${graph}${auxiliary.length ? `<details data-fold="auxiliary"><summary>辅助操作 · ${auxiliary.length}</summary><div class="map-aux">${auxiliary.join('')}</div></details>` : ''}</div>`;
}

export function mountMaps(panel, previous = new Map()) {
  for (const map of panel.querySelectorAll('.plan-map')) {
    const view = map.querySelector('.map-window');
    if (!view) continue;
    const world = view.querySelector('.map-world'), size = world.parentElement;
    const saved = previous.get(map.dataset.map);
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
    zoom(saved?.scale || 1);
    if (saved) view.scrollTo(saved.left,saved.top);
    map.querySelector('.map-tools').onclick = event => {
      const control = event.target.closest('[data-zoom]');
      if (control) zoom(Number(control.dataset.zoom) ? Number(map.dataset.scale)*1.25**Number(control.dataset.zoom) : 1);
    };
    view.onwheel = event => {
      if (!event.ctrlKey && !event.metaKey) return;
      event.preventDefault();
      const rect=view.getBoundingClientRect();
      zoom(Number(map.dataset.scale)*Math.exp(-event.deltaY*.008),event.clientX-rect.left,event.clientY-rect.top);
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
    return [map.dataset.map,{left:view.scrollLeft,top:view.scrollTop,scale:Number(map.dataset.scale)}];
  }));
}
