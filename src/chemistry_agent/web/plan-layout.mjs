// Geometry is in world pixels. Zoom and the viewport never change the dependency graph.
export const CARD = {width:184, height:82, column:234, row:100};
export const kindName = kind => ({vasp:'VASP 计算',model:'结构建模',analysis:'数据分析'})[kind] || '计算步骤';

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
    node.group = heads.has(node.id) || shared ? node.id : lead.group;
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
  return {byId,vertices,groups:[...ordered.values()].sort((a,b)=>Number(!heads.has(a.id))-Number(!heads.has(b.id)))};
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

export function layoutGraph(graph, folded, moves = new Map(), groupId = null, compact = false) {
  const candidates = groupId ? graph.vertices.filter(n=>n.group===groupId).map(n=>({
    id:n.id, parents:n.parents.filter(p=>graph.byId.get(p).group===groupId)
  })) : graph.vertices;
  const visible=reachableVertices(candidates,folded), trees=new Map(), incoming=new Set();
  for (const node of graph.vertices) {
    if (!visible.has(node.id)) continue;
    const parent = graph.byId.get(node.primary);
    if (!parent || parent.group !== node.group || !visible.has(parent.id) || folded.has(parent.id)) continue;
    if (!trees.has(parent.id)) trees.set(parent.id,[]);
    trees.get(parent.id).push(node.id); incoming.add(node.id);
  }
  const points=new Map(), lanes=[];
  const baseDepth=groupId && graph.byId.has(groupId) ? graph.byId.get(groupId).depth : 0;
  let y=12, right=0, bottom=0;
  for (const group of graph.groups) {
    const members=group.members.filter(id=>visible.has(id));
    if (!members.length) continue;
    const banded = graph.groups.length > 1;
    const start=y, stack=members.filter(id=>!incoming.has(id)).reverse();
    if (banded) y+=32;
    while (stack.length) {
      const id=stack.pop(), node=graph.byId.get(id), move=moves.get(id);
      const inset=compact && banded ? 38 : 18;
      const depth=compact && banded ? node.depth-graph.byId.get(group.id).depth : node.depth-baseDepth;
      const x=Math.max(12,inset+depth*CARD.column+(move?.x || 0));
      const top=Math.max(10,y+(move?.y || 0));
      points.set(id,{x,y:top});
      right=Math.max(right,x+CARD.width); bottom=Math.max(bottom,top+CARD.height);
      const children=trees.get(id);
      if (children?.length) {
        for (let i=children.length-1;i>=0;i--) stack.push(children[i]);
      } else y+=CARD.row;
    }
    if (banded) { lanes.push({group,start,height:y-start,count:members.length}); y+=10; }
  }
  return {points,lanes,width:Math.ceil(right+24),height:Math.ceil(Math.max(bottom+20,y))};
}

export function edgePath(a,b,rail=false) {
  const from=a.y+CARD.height/2,to=b.y+CARD.height/2;
  if(rail) {
    const bend=Math.min(14,Math.abs(to-from)/2), sign=Math.sign(to-from), x=Math.min(a.x,b.x)-19;
    return `M${a.x} ${from} H${x+bend} Q${x} ${from},${x} ${from+sign*bend} V${to-sign*bend} Q${x} ${to},${x+bend} ${to} H${b.x}`;
  }
  if(b.x+CARD.width<a.x) {
    const x=b.x+CARD.width,mid=(x+a.x)/2;
    return `M${a.x} ${from} C${mid} ${from},${mid} ${to},${x} ${to}`;
  }
  const x=a.x+CARD.width, y=a.y+CARD.height/2, end=b.y+CARD.height/2;
  const bend=Math.max(30,Math.min(130,Math.abs(b.x-x)*.5));
  return `M${x} ${y} C${x+bend} ${y},${b.x-bend} ${end},${b.x} ${end}`;
}

// Leave a free drop untouched. If it would cover another card, pick the nearest
// clear position from the obstacle boundaries, rather than pushing the whole lane.
export function clearDrop(points,id) {
  const desired=points.get(id), others=[...points].filter(([key])=>key!==id).map(([,p])=>p);
  const w=CARD.width+6,h=CARD.height+6;
  if(!others.some(p=>Math.abs(p.x-desired.x)<w && Math.abs(p.y-desired.y)<h)) return desired;
  const xs=new Set([desired.x,12]);
  for(const p of others) {if(p.x-w>=12) xs.add(p.x-w);xs.add(p.x+w);}
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
    if(lo!=null && lo<y && y<hi) y=lo>=10 && y-lo<hi-y ? lo : hi;
    const dist=dx+(y-desired.y)**2;
    if(dist<best) {result={x,y};best=dist;}
  }
  return result;
}
