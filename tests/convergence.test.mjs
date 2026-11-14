import test from 'node:test';
import assert from 'node:assert/strict';
import {convergenceData,linearScale,plotScale,formatTick,zoomRange,lineSegments} from '../src/chemistry_agent/web/convergence-model.mjs';

test('ionic differences ignore gaps, partial/missing force and non-finite values',()=>{
  const d=convergenceData({points:[[1,-10],[2,-11],[4,-12],[5,NaN]],monitor:{force_points:[[2,.3],[3,.9],[4,0]]}});
  assert.equal(d.points[0].delta,null);assert.equal(d.points[1].delta,1);assert.equal(d.points[2].delta,null);
  assert.equal(d.points[0].force,undefined);assert.equal(d.points[2].force,0);assert.equal(d.points.length,3);
});
test('scheduler completion and small energy differences never imply convergence',()=>{
  assert.equal(convergenceData({terminal:true,points:[[1,-1],[2,-1.0000001]]}).status,'已结束');
  assert.equal(convergenceData({monitor:{ionic_converged:true}}).status,'已收敛');
  assert.equal(convergenceData({points:[[1,-1]],scheduler_state:'RUNNING'}).status,'进行中');
});
test('restart replaces old segment instead of connecting unrelated energies',()=>{
  assert.deepEqual(convergenceData({points:[[1,5],[2,4],[1,9],[2,8]]}).points.map(p=>p.energy),[9,8]);
});
test('zoomed energy domain reveals late changes and keeps dynamic padding',()=>{
  const full=linearScale([-10,-100,-100.00001,-100.00002]),late=linearScale([-100.00001,-100.00002]);
  assert(full.max-full.min>90);assert(late.max-late.min<.0001);
  assert(late.max> -100.00001 && late.min< -100.00002);
  assert.equal(new Set(late.ticks.map(v=>formatTick(v,late))).size,late.ticks.length);
  const flat=linearScale([-100,-100]);assert(flat.max>flat.min);
});
test('log zeros are not substituted with fake epsilon; force threshold uses matching basis',()=>{
  const log=plotScale([{delta:0},{delta:1e-8}],'delta');
  assert.equal(log.transform(0),null);assert.equal(log.transform(1e-8),-8);
  assert(log.min < -8);assert.equal(log.guides.length,2);
  assert.equal(plotScale([{force:.02}],'force',{ediffg:-.01,force_basis:'free_atoms'}).guides[0].value,.01);
  assert.equal(plotScale([{force:.02}],'force',{ediffg:-.01,force_basis:'raw_directional_constraints'}).guides.length,0);
});
test('zoom is bounded; missing values split paths instead of implying sampled values',()=>{
  assert.deepEqual(zoomRange([0,99],100,1,.2),[79,99]);
  assert.deepEqual(zoomRange([79,99],100,1,100),[0,99]);
  assert.deepEqual(zoomRange([0,0],1,.5,.5),[0,0]);
  assert.equal(lineSegments([{step:1,force:1},{step:2},{step:3,force:2}],'force',v=>v,v=>v),'M1.00,1.00  M3.00,2.00');
});

test('trajectory target uses the chosen ionic step, not its array offset or always frame one',async()=>{
  const {selectedIonicPoint}=await import('../src/chemistry_agent/web/convergence-model.mjs');
  const points=[{step:83},{step:87},{step:98},{step:119}];
  assert.equal(selectedIonicPoint(points,[1,2]).step,98);
  assert.equal(selectedIonicPoint(points,[1,2],87).step,87);
  assert.equal(selectedIonicPoint(points,[1,2],119).step,98);
  assert.equal(selectedIonicPoint([],[0,0]),null);
});

test('MD exposes temperature at step zero, real elapsed time and sampled gaps without claiming convergence',()=>{
  const data=convergenceData({terminal:true,monitor:{kind:'md',engine:'LAMMPS',ionic_converged:true,md_points:[{step:0,temperature:300,time_ps:0},{step:20,temperature:302,time_ps:.01}]}});
  assert.equal(data.status,'已结束');assert.equal(data.isMD,true);assert.equal(data.points[0].step,0);
  assert.equal(data.points[1].time_ps,.01);assert.equal(data.points[1].delta,undefined);
  assert.equal(lineSegments(data.points,'temperature',v=>v,v=>v,true),'M0.00,300.00 L20.00,302.00');
  const scale=plotScale(data.points,'temperature',{kind:'md',target_temperature:300});
  assert.equal(scale.guides[0].value,300);assert(scale.min<300 && scale.max>302);
});

test('long MD histories do not exceed the JavaScript function argument limit',()=>{
  const values=Array.from({length:200000},(_,i)=>300+Math.sin(i));
  const scale=linearScale(values);assert(scale.min<299 && scale.max>301);
});

test('a running scheduler allocation does not claim that MD has started without samples',()=>{
  assert.equal(convergenceData({scheduler_state:'RUNNING',monitor:{kind:'md',md_points:[]}}).status,'等待 MD 记录');
  assert.equal(convergenceData({scheduler_state:'RUNNING',monitor:{kind:'md',stage:'minimization',md_points:[]}}).status,'准备中');
});
