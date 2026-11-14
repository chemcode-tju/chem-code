import test from 'node:test';
import assert from 'node:assert/strict';
import {profilePoints,selectedPath,resultURL} from '../src/chemistry_agent/web/registered-results.mjs';

test('NEB iteration and image axes remain independent, including step zero',()=>{
  const first={step:0,images:[{index:0,relative_energy:0},{index:1,relative_energy:2}]};
  const last={step:3,images:[{index:0,relative_energy:0},{index:1,relative_energy:1}]};
  const data={snapshots:[first,last]};
  assert.equal(selectedPath(data,0),first);
  assert.equal(selectedPath(data,null),last);
  assert.deepEqual(profilePoints(first),[[0,0],[1,2]]);
  assert.deepEqual(profilePoints(last),[[0,0],[1,1]]);
});
test('nested registered file references use query parameters without losing path',()=>{
  const url=new URL(resultURL('/api/step','job','neb','paths/step_001/image_02.vasp'),'http://localhost');
  assert.equal(url.searchParams.get('file'),'paths/step_001/image_02.vasp');
  assert.equal(url.pathname,'/api/step/results');
});

test('NEB defaults to an image profile; one mode selects one chart and the right axis',async()=>{
  const {nebChart}=await import('../src/chemistry_agent/web/registered-results.mjs');
  const row={step:0,force:.6,peak_energy:2,images:[
    {index:0,relative_energy:0,force:null},
    {index:1,relative_energy:2,force:.6},
    {index:2,relative_energy:1,force:null},
  ]};
  const data={snapshots:[row,{...row,step:1,force:.1}],energy_label:'TOTEN',force_target:.02};
  const profile=nebChart(data,0);
  assert.equal(profile.mode,'profile');assert.equal(profile.action,'image');
  assert.deepEqual(profile.points,[[0,0],[1,2],[2,1]]);
  const force=nebChart(data,0,'image-force');
  assert.deepEqual(force.points,[[1,.6]]);assert.equal(force.x_label,'路径像');
  assert.equal(force.target,.02);assert.equal(force.unit,'eV/Å');
  const history=nebChart(data,0,'force-history');
  assert.equal(history.action,'iteration');assert.equal(history.x_label,'路径优化步');
  assert.deepEqual(history.points,[[0,.6],[1,.1]]);
  assert.equal(nebChart(data,0,'energy-history').action,'iteration');
});
test('absent per-image force is not fabricated or confused with path Fmax',async()=>{
  const {nebChart}=await import('../src/chemistry_agent/web/registered-results.mjs');
  const data={snapshots:[{step:0,force:.2,peak_energy:1,images:[{index:0,relative_energy:0},{index:1,relative_energy:1}]}]};
  const result=nebChart(data,0,'image-force');
  assert.equal(result.mode,'profile');
  assert.equal(result.modes.some(([key])=>key==='image-force'),false);
  assert.equal(result.modes.some(([key])=>key==='force-history'),true);
});

test('endpoint atomic forces are separately selectable and have no NEB threshold',async()=>{
  const {nebChart}=await import('../src/chemistry_agent/web/registered-results.mjs');
  const data={force_target:.02,snapshots:[{step:0,force:.5,peak_energy:2,images:[
    {index:0,relative_energy:0,force:null,endpoint_reference:{atomic_fmax:.04,energy:-10}},
    {index:1,relative_energy:2,force:.5},
    {index:2,relative_energy:1,force:null,endpoint_reference:{atomic_fmax:.03,energy:-9}},
  ]}]};
  const result=nebChart(data,0,'endpoint-force');
  assert.deepEqual(result.points,[[0,.04],[2,.03]]);
  assert.equal(result.target,null);
  assert.equal(result.action,'image');
  assert.equal(result.y_label,'原子 Fmax');
  assert.deepEqual(nebChart(data,0,'image-force').points,[[1,.5]]);
  assert.equal(nebChart(data,0).mode,'profile');
});

test('force history describes the path maximum rather than a previously selected image force',async()=>{
  const {nebSelection,historyImage,nebChart}=await import('../src/chemistry_agent/web/registered-results.mjs');
  const row={step:0,force:2.830984871218285,peak_energy:1,images:[
    {index:0,energy:-10,relative_energy:0,force:null},
    {index:1,energy:-9.8,relative_energy:.2,force:1.953274978970915},
    {index:3,energy:-9,relative_energy:1,force:2.830984871218285},
  ]};
  const data={snapshots:[row],energy_label:'TOTEN'};
  const history=nebSelection(data,0,1,'force-history');
  assert.match(history,/优化步 0.*路径最大投影力 2.8310 eV\/Å.*对应像 3/);
  assert.doesNotMatch(history,/1.9533|TOTEN/);
  assert.equal(historyImage(row,'force-history'),3);
  assert.match(nebSelection(data,0,1,'image-force'),/优化步 0 · 像 1 · 投影最大力 1.9533/);
  assert.match(nebSelection(data,0,0,'image-force'),/无记录/);
  assert.equal(nebChart(data,0,'image-force').y_label,'像 Fmax');
  assert.equal(nebChart(data,0,'force-history').y_label,'路径 Fmax');
  assert.equal(historyImage({...row,force:3},'force-history'),null);
  assert.match(nebSelection(data,0,1,'energy-history'),/路径相对能量峰值 1.0000 eV.*对应像 3/);
});
