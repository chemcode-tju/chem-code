import assert from 'node:assert/strict';
import test from 'node:test';
import {buildGraph, CARD, clearDrop, edgePath, edgeRoute, layoutGraph, overviewFolds, reachableVertices, taskLabel} from '../src/chemistry_agent/web/plan-layout.mjs';
import {graphLayers} from '../src/chemistry_agent/web/general-plan.mjs';

const node=(id,parents=[],auxiliary=false)=>({id,title:id,parents,auxiliary});

test('folding stops only paths through that calculation, including at shared joins', () => {
  const vertices=[node('a'),node('b'),node('left',['a']),node('join',['left','b']),node('end',['join'])];
  assert.deepEqual([...reachableVertices(vertices,new Set(['a']))], ['a','b','join','end']);
  assert.deepEqual([...reachableVertices(vertices,new Set(['a','b']))], ['a','b']);
  assert.deepEqual([...reachableVertices(vertices,new Set(['a','join']))], ['a','b','join']);
});

test('ten thousand sequential steps sort and lay out without recursion', () => {
  const nodes=Array.from({length:10000},(_,i)=>node(i,i?[i-1]:[]));
  assert.equal(graphLayers(nodes.map(n=>({...n,depends_on:n.parents}))).length,10000);
  const graph=buildGraph(nodes),shown=layoutGraph(graph,new Set([3000]));
  assert.equal(shown.points.size,3001);
  assert.deepEqual(shown.points.get(3000),{x:64+3000*CARD.column,y:80});
});

test('interleaved sites of five facets occupy disjoint lanes and stay with the same upstream site',()=>{
  const faces=['111','100','110','211','310'],ids=faces.map(f=>'f'+f+'_clean'),nodes=[node('bulk'),...ids.map(id=>node(id,['bulk']))];
  for(let i=0;i<18;i++) for(const f of faces) {
    const site='f'+f+'_s'+i;
    nodes.push(node(site+'_model',['f'+f+'_clean'],true),node(site+'_relax',[site+'_model']),node(site+'_vib',[site+'_relax']));
  }
  const graph=buildGraph(nodes), overview=overviewFolds(graph), all=layoutGraph(graph,new Set());
  assert.deepEqual([...overview],ids);
  assert.equal(layoutGraph(graph,overview).points.size,6);
  for (const [i,g] of graph.groups.slice(1).entries()) {
    assert.equal(g.members.length,37);
    const ys=g.members.map(id=>all.points.get(id).y), next=graph.groups[i+2];
    if(next) assert(Math.max(...ys)+CARD.height < all.points.get(next.id).y);
    assert.deepEqual(graph.byId.get('f'+faces[i]+'_s5_relax').parents,[ids[i]]);
    assert.equal(all.points.get('f'+faces[i]+'_s5_relax').y,all.points.get('f'+faces[i]+'_s5_vib').y);
  }
  overview.delete(ids[2]);
  const unfolded=layoutGraph(graph,overview);
  assert.equal(unfolded.points.size,42);
  assert(!unfolded.points.has('f211_s0_relax'));
  const isolated=layoutGraph(graph,new Set(['bulk']),new Map(),ids[2]);
  assert.equal(isolated.points.size,37);assert.equal(isolated.points.get(ids[2]).x,64);
  assert.deepEqual(buildGraph(nodes,true).byId.get('f110_s5_relax').parents,['f110_s5_model']);
});

test('moving a card adjusts both adjacent endpoints without changing other positions or the automatic layout',()=>{
  const g=buildGraph([node('a'),node('b',['a']),node('c',['b'])]);
  const plain=layoutGraph(g,new Set()), pinned=layoutGraph(g,new Set(),new Map([['b',{x:50,y:150}]]));
  assert.deepEqual(pinned.points.get('c'),plain.points.get('c'));
  const a=pinned.points.get('a'), b=pinned.points.get('b'), c=pinned.points.get('c');
  assert.equal(b.y,plain.points.get('b').y+150);
  assert.ok(edgePath(a,b).endsWith('L'+b.x+' '+(b.y+CARD.height/2)));
  assert.ok(edgePath(b,c).startsWith('M'+(b.x+CARD.width)+' '+(b.y+CARD.height/2)));
  assert.ok(pinned.height>=b.y+CARD.height);
});

test('scientific task IDs remain searchable while human-supplied titles are preserved',()=>{
  assert.match(taskLabel(node('f110_s05_relax')),/110.*05.*优化/);
  assert.equal(taskLabel({id:'f110_s05_relax',title:'My CO run'}),'My CO run');
  assert.equal(taskLabel(node('nonstandard_stage')),'nonstandard_stage');
});

test('a drop near two neighbors settles into the closest gap without covering either card',()=>{
  const points=new Map([['drag',{x:308,y:142}],['right',{x:486,y:112}],['below',{x:252,y:212}]]);
  const clear=clearDrop(points,'drag');
  for (const [id,p] of points) if(id!=='drag') assert(Math.abs(p.x-clear.x)>=CARD.width+6 || Math.abs(p.y-clear.y)>=CARD.height+6);
  points.set('drag',clear);assert.deepEqual(clearDrop(points,'drag'),clear);
});

test('nested group metadata filters real steps without adding boxes or changing dependency edges',()=>{
  const data=[node('x'), ...['q','z','m','n'].map(id=>node(id,['x'])),node('out',['q','z','m','n'])];
  [['Heavy water','10 ps'],['n-octane','10 ps'],['Heavy water','20 ps'],['n-octane','10 ps']].forEach((path,i)=>data[i+1].groupPath=path);
  const graph=buildGraph(data), folder=[...graph.folders.values()].find(f=>f.path.join('/')==='n-octane/10 ps');
  const layout=layoutGraph(graph,new Set());
  assert.equal(layout.points.size,data.length);
  assert.equal(layout.headers.size,0);assert.equal(layout.lanes.length,0);
  assert.deepEqual(graph.byId.get('out').parents,['q','z','m','n']);
  const filtered=layoutGraph(graph,new Set(),new Map(),folder.id);
  assert.deepEqual([...filtered.points.keys()],['z','n']);
  assert.deepEqual(layoutGraph(graph,new Set(),new Map(),null,true).points,layout.points);
});

test('wide unlabeled forks get neutral batches while shared results remain outside them',()=>{
  const leaves=Array.from({length:29},(_,i)=>node('op'+i,['base']));
  const graph=buildGraph([node('base'),...leaves,node('join',leaves.map(n=>n.id))]);
  assert.deepEqual([...graph.folders.values()].map(g=>[g.title,g.members.length]),[['1–12',12],['13–24',12],['25–29',5]]);
  assert(graph.folders.size && [...graph.folders.values()].every(g=>g.batch));
  assert.deepEqual(graph.byId.get('join').folderIds,[]);
  const closed=layoutGraph(graph,overviewFolds(graph),new Map(),null,false,new Set(graph.folders.keys()));
  assert.equal(closed.points.size,1);assert.equal(closed.headers.size,0);
  assert.equal(layoutGraph(graph,new Set()).points.size,31);
});

test('a later anonymous sweep is still paged in a workflow with other explicitly named branches',()=>{
  const nodes=[node('a'),node('small',['a']),node('sweep',['a']),
    ...Array.from({length:30},(_,i)=>node('t'+i,['sweep']))];
  nodes[1].groupPath=['Control'];
  const graph=buildGraph(nodes);
  assert.deepEqual([...graph.folders.values()].filter(g=>g.batch).map(g=>g.members.length),[12,12,6]);
  assert.equal(graph.byId.get('a').folderIds.length,0);
});


test('the critical path is horizontal, forks align by rank, and skip edges avoid intervening cards',()=>{
  const graph=buildGraph([node('a'),node('b',['a']),node('side',['a']),node('c',['b']),node('join',['c','a','side'])]);
  const layout=layoutGraph(graph,new Set());
  const {points}=layout;
  for(const id of ['b','c','join'])assert.equal(points.get(id).y,points.get('a').y);
  assert.equal(points.get('b').x,points.get('side').x);
  assert(points.get('side').y>=points.get('b').y+CARD.row);
  const route=edgeRoute('a','join',layout), path=edgePath(points.get('a'),points.get('join'),route);
  assert(route.viaY<points.get('b').y || route.viaY>points.get('b').y+CARD.height);
  assert.match(path,/Q/);assert.doesNotMatch(path,/C|NaN|Infinity/);
  assert.ok(path.endsWith(`L${points.get('join').x} ${points.get('join').y+CARD.height/2}`));
});


test('vertical presentation preserves edges and keeps parallel branches in separate columns',()=>{
  const graph=buildGraph([node('a'),node('b',['a']),node('side',['a']),node('end',['b','a'])]);
  const layout=layoutGraph(graph,new Set(),new Map(),null,'TB');
  assert.equal(layout.points.get('a').x,layout.points.get('end').x);
  assert.equal(layout.points.get('b').y,layout.points.get('side').y);
  const route=edgeRoute('a','end',layout);
  assert.equal(route.direction,'TB');assert(route.viaX<layout.points.get('b').x);
  assert.doesNotMatch(edgePath(layout.points.get('a'),layout.points.get('end'),route),/C|NaN|Infinity/);
});


test('dragging beyond a grid row keeps a clear forward connection local',()=>{
  for(const direction of ['TB','LR']) {
    const a={x:400,y:400},b=direction==='TB'?{x:460,y:400+CARD.row+60}:{x:400+CARD.column+60,y:460};
    const layout={direction,points:new Map([['a',a],['b',b],['remote',{x:64,y:80}]]),ranks:new Map([['a',0],['b',1]])};
    const route=edgeRoute('a','b',layout);
    assert.equal(route?.viaX,undefined);assert.equal(route?.viaY,undefined);
    const path=edgePath(a,b,route);assert.doesNotMatch(path,/NaN|Infinity/);
  }
});

test('a backwards drag uses a nearby corridor rather than the distant graph boundary',()=>{
  const layout={direction:'TB',points:new Map([['a',{x:500,y:600}],['b',{x:500,y:400}],['remote',{x:64,y:80}]]),ranks:new Map([['a',0],['b',1]])};
  const route=edgeRoute('a','b',layout);
  assert.equal(route.viaX,476);
  assert.doesNotMatch(edgePath(layout.points.get('a'),layout.points.get('b'),route),/NaN|Infinity/);
});
