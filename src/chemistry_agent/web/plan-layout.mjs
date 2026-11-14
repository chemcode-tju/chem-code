import {indexFolders, inSection} from './plan-folders.mjs';

// Geometry is in world pixels. Zoom and the viewport never change the dependency graph.
export const CARD = {width:248, height:88, column:344, row:144};
export const kindName = kind => ({task:'研究步骤',job:'Slurm 任务',vasp:'VASP 计算',model:'结构建模',analysis:'数据分析'})[kind] || '计算步骤';

export function taskLabel({id, title}) {
  if (title && title !== id) return title;
  const face = /^f(\d{3})(?:_s(\d+))?_(model|clean|relax|vib_model|vib|thermo)$/.exec(id);
  const stages = {model:'建模',clean:'洁净表面',relax:'结构优化',vib_model:'振动模型',vib:'振动计算',thermo:'热化学校正'};
  if (face) return [`(${face[1]})`, face[2] && `位点 ${face[2]}`, stages[face[3]]].filter(Boolean).join(' · ');
  return ({bulk_model:'体相建模',bulk_relax:'体相优化',gas_model:'气相建模',gas_relax:'气相优化',gas_vib_model:'气相振动模型',gas_vib:'气相振动计算',gas_thermo:'气相热化学',summary:'结果汇总'})[id] || title || id;
}

// Nodes arrive in topological order. In the calculation view, an edge spans only
// intervening auxiliary operations; the all-steps view always uses the original DAG.
export function buildGraph(nodes, all = false) {
  const byId = new Map(), nearest = new Map();
  for (const node of nodes) {
    const parents = [...new Set(node.parents.flatMap(id => nearest.get(id) || []))];
    if (!all && node.auxiliary) { nearest.set(node.id, parents); continue; }
    nearest.set(node.id, [node.id]);
    const primary = parents.reduce((best,id) => best == null || byId.get(id).depth > byId.get(best).depth ? id : best, null);
    const depth = primary == null ? 0 : byId.get(primary).depth + 1;
    const row = {...node, parents, primary, depth, children:[], tree:[]};
    byId.set(node.id, row);
    for (const id of parents) byId.get(id).children.push(node.id);
    if (primary != null) byId.get(primary).tree.push(node.id);
  }
  const vertices = [...byId.values()], roots = vertices.filter(n=>n.primary == null);
  const heads = new Set(roots.map(n=>n.id)), groups = new Map();
  // The first fork of each trunk defines a set of lanes. Within each lane,
  // the next forks (e.g. adsorption sites) get contiguous subtrees, not new lanes.
  for (let node of roots) {
    while (node.tree.length === 1) node = byId.get(node.tree[0]);
    if (node.tree.length <= 12) for (const id of node.tree) heads.add(id);
  }
  for (const node of vertices) {
    const lead = byId.get(node.primary);
    const shared = lead && node.parents.some(id => byId.get(id).depth === lead.depth && byId.get(id).group !== lead.group);
    const leavesFolder = lead?.groupPath?.length && !node.groupPath?.length;
    node.group = heads.has(node.id) || shared || leavesFolder ? node.id : lead.group;
    if (!groups.has(node.group)) groups.set(node.group,{id:node.id,title:taskLabel(node),members:[]});
    groups.get(node.group).members.push(node.id);
  }
  // Follow a branch to the end before moving to the next sibling, even if the
  // server interleaves node definitions. Preserve this order across folds.
  const ordered = new Map(), stack = roots.map(n=>n.id).reverse();
  while (stack.length) {
    const node=byId.get(stack.pop());
    if (!ordered.has(node.group)) ordered.set(node.group,groups.get(node.group));
    for (let i=node.tree.length-1;i>=0;i--) stack.push(node.tree[i]);
  }
  const lanes=[...ordered.values()].sort((a,b)=>Number(!heads.has(a.id))-Number(!heads.has(b.id)));
  return {byId,vertices,groups:lanes,...indexFolders(vertices,byId,lanes)};
}

export function reachableVertices(vertices, folded) {
  const visible = new Set();
  for (const {id,parents} of vertices)
    if (!parents.length || parents.some(p=>visible.has(p) && !folded.has(p))) visible.add(id);
  return visible;
}

export function overviewFolds(graph) {
  const folds = new Set(), heads = new Set(graph.groups.map(g=>g.id));
  for (const node of graph.vertices) {
    // Keep trunk-to-lane edges open. Fold the fan-out inside a branch.
    const laneBridge=node.tree.some(id=>heads.has(id));
    // A shared reference can independently reveal dozens of terminal analyses.
    if (!laneBridge && (node.tree.length > 1 || node.children.length > 8)) folds.add(node.id);
  }
  return folds;
}

export const FOLDER_HEIGHT = 36;

// One rank per dependency depth; the longest continuation stays on the same row.
// Group metadata remains available for filtering, never introduces container nodes.
export function layoutGraph(graph, folded, moves = new Map(), scope = null, direction = 'LR') {
  const candidates = graph.vertices.filter(node => inSection(node, scope));
  const ids = new Set(candidates.map(node => node.id));
  const visible = reachableVertices(candidates.map(node => ({
    ...node, parents:node.parents.filter(id => ids.has(id))
  })), folded);
  const vertices = candidates.filter(node => visible.has(node.id));
  const trees = new Map(), roots = [], ranks = new Map();
  for (const node of vertices) {
    const parents = node.parents.filter(id => visible.has(id) && !folded.has(id));
    const parent = parents.reduce((best, id) => best == null || ranks.get(id) > ranks.get(best) ? id : best, null);
    ranks.set(node.id, parent == null ? 0 : ranks.get(parent) + 1);
    if (parent == null) roots.push(node.id);
    else {
      if (!trees.has(parent)) trees.set(parent, []);
      trees.get(parent).push(node.id);
    }
  }
  const lengths = new Map();
  for (const node of [...vertices].reverse()) {
    const children = trees.get(node.id) || [];
    children.sort((a,b) => lengths.get(b) - lengths.get(a));
    lengths.set(node.id, 1 + children.reduce((max,id) => Math.max(max,lengths.get(id)),0));
  }
  const points = new Map(), stack = [...roots].reverse();
  let row = 0, right = 0, bottom = 0;
  while (stack.length) {
    const id = stack.pop(), move = moves.get(id), children = trees.get(id) || [];
    const x = Math.max(64, 64 + (direction==='TB'?row:ranks.get(id)) * CARD.column + (move?.x || 0));
    const y = Math.max(80, 80 + (direction==='TB'?ranks.get(id):row) * CARD.row + (move?.y || 0));
    points.set(id, {x,y});
    right = Math.max(right, x + CARD.width); bottom = Math.max(bottom, y + CARD.height);
    if (children.length) stack.push(...[...children].reverse());
    else row++;
  }
  return {points, ranks, direction, lanes:[], headers:new Map(), links:[], width:Math.ceil(right+40), height:Math.ceil(bottom+48)};
}

function roundedPath(points, radius = 12) {
  const clean = points.filter((point,i) => !i || point.x !== points[i-1].x || point.y !== points[i-1].y);
  let path = `M${clean[0].x} ${clean[0].y}`;
  for (let i=1; i<clean.length-1; i++) {
    const before=clean[i-1], point=clean[i], after=clean[i+1];
    const incoming=Math.hypot(point.x-before.x,point.y-before.y), outgoing=Math.hypot(after.x-point.x,after.y-point.y);
    const r=Math.min(radius,incoming/2,outgoing/2);
    const start={x:point.x+(before.x-point.x)*r/incoming,y:point.y+(before.y-point.y)*r/incoming};
    const end={x:point.x+(after.x-point.x)*r/outgoing,y:point.y+(after.y-point.y)*r/outgoing};
    path+=` L${start.x} ${start.y} Q${point.x} ${point.y} ${end.x} ${end.y}`;
  }
  const end=clean.at(-1);
  return path+` L${end.x} ${end.y}`;
}

export function edgePath(a,b,route = null) {
  if (route?.direction==='TB') {
    const start={x:a.x+CARD.width/2,y:a.y+CARD.height},end={x:b.x+CARD.width/2,y:b.y};
    if (Number.isFinite(route.viaX)) return roundedPath([start,{x:start.x,y:start.y+18},
      {x:route.viaX,y:start.y+18},{x:route.viaX,y:end.y-32},{x:end.x,y:end.y-32},end]);
    const mid=(start.y+end.y)/2;
    return roundedPath([start,{x:start.x,y:mid},{x:end.x,y:mid},end]);
  }
  const start={x:a.x+CARD.width,y:a.y+CARD.height/2}, end={x:b.x,y:b.y+CARD.height/2};
  if (Number.isFinite(route?.viaY) || end.x-start.x<32) {
    const viaY=route?.viaY ?? Math.min(a.y,b.y)-32;
    return roundedPath([start,{x:start.x+24,y:start.y},{x:start.x+24,y:viaY},
      {x:end.x-24,y:viaY},{x:end.x-24,y:end.y},end]);
  }
  const mid=(start.x+end.x)/2;
  return roundedPath([start,{x:mid,y:start.y},{x:mid,y:end.y},end]);
}

// Skip-rank edges use the quiet strip above the grid, clear of intermediate cards.
export function edgeRoute(source, target, layout) {
  const a=layout.points.get(source), b=layout.points.get(target);
  if (!a || !b) return null;
  if (layout.direction==='TB') {
    if (b.y-a.y <= CARD.row+1 && b.y-a.y >= CARD.height+32) return {direction:'TB'};
    const left=Math.min(...[...layout.points.values()].map(point=>point.x));
    return {direction:'TB',viaX:Math.max(8,left-24-(layout.ranks.get(source)%3)*12)};
  }
  if (b.x-a.x <= CARD.column+1 && b.x-a.x >= CARD.width+32) return null;
  const top=Math.min(...[...layout.points.values()].map(point=>point.y));
  return {viaY:Math.max(8,top-30-(layout.ranks.get(source)%3)*12)};
}

// Leave a free drop untouched. If it would cover another card, pick the nearest
// clear position from the obstacle boundaries, rather than pushing the whole lane.
export function clearDrop(points,id) {
  const desired=points.get(id), others=[...points].filter(([key])=>key!==id).map(([,p])=>p);
  const w=CARD.width+6,h=CARD.height+6;
  if(!others.some(p=>Math.abs(p.x-desired.x)<w && Math.abs(p.y-desired.y)<h)) return desired;
  const xs=new Set([desired.x,64]);
  for(const p of others) {if(p.x-w>=64) xs.add(p.x-w);xs.add(p.x+w);}
  let best=Infinity, result=desired;
  for(const x of [...xs].sort((a,b)=>Math.abs(a-desired.x)-Math.abs(b-desired.x))) {
    const dx=(x-desired.x)**2;if(dx>=best) break;
    const ys=others.filter(p=>Math.abs(p.x-x)<w).map(p=>p.y).sort((a,b)=>a-b);
    let lo=null,hi=null,y=desired.y;
    for(const v of ys) {
      if(hi==null || v-h>=hi) {
        if(lo!=null && lo<y && y<hi) break;
        lo=v-h;hi=v+h;
      } else hi=v+h;
    }
    if(lo!=null && lo<y && y<hi) y=lo>=80 && y-lo<hi-y ? lo : hi;
    const dist=dx+(y-desired.y)**2;
    if(dist<best) {result={x,y};best=dist;}
  }
  return result;
}
