const test=require('node:test');
const assert=require('node:assert/strict');
global.StructureParser=require('../../src/chemistry_agent/web/structure/parser.js');
global.StructureTrajectory=require('../../src/chemistry_agent/web/structure/trajectory-store.js');
const {open}=require('../../src/chemistry_agent/web/structure/import.js');
const poscar=(x,element='Cu')=>`image\n1\n3 0 0\n0 3 0\n0 0 8\n${element}\n1\nSelective dynamics\nDirect\n${x} 0 0 F F F\n`;

test('explicit NEB sequence preserves order, cells, constraints and frame source',async()=>{
  const files=[0,.1,.2].map((x,i)=>new File([poscar(x)],`image_${i}.vasp`));
  const result=await open(files,{sequence:true,name:'NEB step 4'});
  assert.equal(result.name,'NEB step 4');assert.equal(result.store.count,3);
  assert.equal(result.store.complete,true);
  assert.ok(Math.abs((await result.store.get(2)).frame.atoms[0].x-.6)<1e-12);
  assert.deepEqual((await result.store.get(1)).frame.cell,[[3,0,0],[0,3,0],[0,0,8]]);
  assert.match((await result.store.get(1)).text,/F F F/);
  assert.equal(result.store.source.simulationSteps,undefined);
  assert.equal(result.store.source.ionicSteps,undefined);
  await assert.rejects(open(files),/不自动拼接/);
  result.store.dispose();
});
test('incompatible image topology stops playback instead of reordering atoms',async()=>{
  const result=await open([new File([poscar(0)],'a.vasp'),new File([poscar(.2,'H')],'b.vasp')],{sequence:true});
  await assert.rejects(result.store.get(1),/原子数或元素顺序/);
  result.store.dispose();
});
