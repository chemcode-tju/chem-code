import assert from 'node:assert/strict';
import test from 'node:test';
import {projectPoint, reachableVertices} from '../src/chemistry_agent/web/plan-map.mjs';

test('folding stops only paths through that calculation, including at shared joins', () => {
  const vertices = [
    {id:'a', parents:[]}, {id:'b', parents:[]}, {id:'left', parents:['a']},
    {id:'join', parents:['left','b']}, {id:'end', parents:['join']},
  ];
  assert.deepEqual([...reachableVertices(vertices,new Set(['a']))], ['a','b','join','end']);
  assert.deepEqual([...reachableVertices(vertices,new Set(['a','b']))], ['a','b']);
  assert.deepEqual([...reachableVertices(vertices,new Set(['a','join']))], ['a','b','join']);
});

test('long chain can be collapsed iteratively without dropping upstream nodes', () => {
  const vertices=Array.from({length:10000},(_,i)=>({id:i,parents:i?[i-1]:[]}));
  const shown=reachableVertices(vertices,new Set([3000]));
  assert.equal(shown.size,3001);
  assert(shown.has(3000));
});

test('yaw and pitch project depth into screen coordinates on independent axes', () => {
  const front=projectPoint(0,0,0,30,20);
  const back=projectPoint(0,0,100,30,20);
  assert(Math.abs(back.x-front.x-50)<1e-9);
  assert(back.y<front.y);
  assert(back.depth>front.depth);
  assert.deepEqual(projectPoint(10,20,30,0,0),{x:10,y:20,depth:30});
});
