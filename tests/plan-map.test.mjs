import assert from 'node:assert/strict';
import test from 'node:test';
import {buildGraph, CARD, clearDrop, edgePath, layoutGraph, overviewFolds, reachableVertices, taskLabel} from '../src/chemistry_agent/web/plan-layout.mjs';
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
  assert.deepEqual(shown.points.get(3000),{x:18+3000*CARD.column,y:12});
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
  assert.equal(isolated.points.size,37);assert.equal(isolated.points.get(ids[2]).x,18);
  assert.deepEqual(buildGraph(nodes,true).byId.get('f110_s5_relax').parents,['f110_s5_model']);
});

test('moving a card adjusts both adjacent endpoints without changing other positions or the automatic layout',()=>{
  const g=buildGraph([node('a'),node('b',['a']),node('c',['b'])]);
  const plain=layoutGraph(g,new Set()), pinned=layoutGraph(g,new Set(),new Map([['b',{x:50,y:150}]]));
  assert.deepEqual(pinned.points.get('c'),plain.points.get('c'));
  const a=pinned.points.get('a'), b=pinned.points.get('b'), c=pinned.points.get('c');
  assert.equal(b.y,plain.points.get('b').y+150);
  assert.ok(edgePath(a,b).endsWith(','+b.x+' '+(b.y+CARD.height/2)));
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
  assert.deepEqual(clear,{x:296,y:124});
  points.set('drag',clear);assert.deepEqual(clearDrop(points,'drag'),clear);
});
