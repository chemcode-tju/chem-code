const assert=require('node:assert/strict');
global.StructureParser=require('../../src/chemistry_agent/web/structure/parser.js');
global.StructureTrajectory=require('../../src/chemistry_agent/web/structure/trajectory-store.js');
global.StructureLammps=require('../../src/chemistry_agent/web/structure/lammps-metadata.js');
const md=require('../../src/chemistry_agent/web/structure/md-stream.js');
const metadata=text=>StructureLammps.resolve([{name:'input',text}],['1','2']);
const dump=(step=0,columns='id type xs ys zs',rows='2 2 .5 .5 .5\n1 1 0 0 0',box='pp ff pp\n-2 8\n-3 17\n4 34')=>`ITEM: TIMESTEP\n${step}\nITEM: NUMBER OF ATOMS\n2\nITEM: BOX BOUNDS ${box}\nITEM: ATOMS ${columns}\n${rows}\n`;
const parsed=text=>new StructureParser.StructureDocument('x.extxyz',text).frame(0);
const gro=(step,precision=3)=>`time ${step}\n1\n${'1'.padStart(5)}${'SOL'.padEnd(5)}${'OW'.padStart(5)}${'1'.padStart(5)}${[step,.2,.3].map(v=>v.toFixed(precision).padStart(precision+5)).join('')}\n1 2 3\n`;
(async()=>{
  const meta=metadata('units real\ndump_modify v element Cu O sort id');
  let frame=parsed(md.parseDump(dump(),meta));
  assert.deepEqual(frame.atoms.map(a=>a.elem),['Cu','O']);
  assert.deepEqual(frame.atoms.map(a=>a.type),['1','2']);
  assert.deepEqual(frame.origin,[-2,-3,4]);
  assert.equal(frame.atoms[0].mass,undefined,'Do not present elemental fallback as a source mass');
  assert.deepEqual(frame.atoms.map(a=>[a.x,a.y,a.z]),[[0,0,0],[5,10,15]]);
  assert.deepEqual(frame.pbc,[true,false,true]);
  assert.deepEqual(frame.cell,[[10,0,0],[0,20,0],[0,0,30]]);
  // Correct restricted-triclinic bounding-box correction, including a negative tilt.
  frame=parsed(md.parseDump(dump(0,'id type xs ys zs',undefined,'xy xz yz pp pp pp\n-1 13 2\n0 23 -1\n0 30 3'),meta));
  assert.deepEqual(frame.cell,[[11,0,0],[2,20,0],[-1,3,30]]);
  assert.deepEqual([frame.atoms[1].x,frame.atoms[1].y,frame.atoms[1].z],[6,11.5,15]);
  frame=parsed(md.parseDump(dump(0,'id type x y z','2 2 7 8 9\n1 1 1 2 3','abc origin pp ff pp\n10 1 0 1\n0 20 2 2\n0 0 30 3'),meta));
  assert.deepEqual([frame.atoms[0].x,frame.atoms[0].y,frame.atoms[0].z],[0,0,0]);
  assert.deepEqual(frame.cell,[[10,1,0],[0,20,2],[0,0,30]]);
  assert.throws(()=>md.parseDump(dump(0,undefined,'1 1 0 0 0\n1 2 0 0 0'),meta),/ID/);
  assert.throws(()=>md.parseDump(dump(0,'id type x y','1 1 0 0\n2 2 0 0'),meta),/坐标/);
  assert.throws(()=>md.parseDump(dump(0,'id type element x y z','1 1 O 0 0 0\n2 2 O 0 0 0'),meta),/冲突/);
  const explicit=dump(0,'id type element x y z','1 1 C 0 0 0\n2 2 C 1 0 0');
  assert.equal((await md.open(new File([explicit],'x.dump'),'lammps-dump-text')).store.count,1);
  const bytes=new File(['ITEM: UNITS\nreal\nITEM: TIME\n0\n',...Array.from({length:501},(_,i)=>dump(i))],'x.dump');
  bytes.text=()=>{throw Error('Must not read complete trajectory');};
  const opened=await md.open(bytes,'lammps-dump-text',[{name:'in',text:'dump_modify v element Cu O'}]);
  assert.equal(opened.store.count,1);
  await opened.store.scan(()=>{});assert.equal(opened.store.count,501);
  assert.equal((await opened.store.get(500)).frame.atoms[0].elem,'Cu');
  assert.equal(await opened.store.findSimulationStep(0),0);
  assert.equal(await opened.store.findSimulationStep(500),500);
  const sparse=(await md.open(new File([dump(0),dump(20),dump(40)],'sparse.dump'),'lammps-dump-text',[{name:'in',text:'dump_modify v element Cu O'}])).store;
  assert.equal(await sparse.findSimulationStep(20),1);
  assert.equal(await sparse.findSimulationStep(23,{nearest:true}),1);
  await assert.rejects(sparse.findSimulationStep(23),/尚无 MD 步/);
  const restarted=(await md.open(new File([dump(0),dump(20),dump(0),dump(20)],'restart.dump'),'lammps-dump-text',[{name:'in',text:'dump_modify v element Cu O'}])).store;
  assert.equal(await restarted.findSimulationStep(20),3);
  for(let i=0;i<20;i++)await opened.store.get(i);
  assert.equal(opened.store.cache.size,16);
  for(const precision of [3,5]) {
    frame=parsed(md.parseGro(gro(.1,precision)));
    assert.deepEqual([frame.atoms[0].x,frame.atoms[0].y,frame.atoms[0].z],[1,2,3]);
    assert.deepEqual(frame.cell,[[10,0,0],[0,20,0],[0,0,30]]);
  }
  const groFile=new File(Array.from({length:500},(_,i)=>gro(i/100)),'x.gro');
  groFile.text=()=>{throw Error('Must not read complete GRO');};
  const groStore=(await md.open(groFile,'gromacs')).store;
  await groStore.scan(()=>{});assert.equal(groStore.count,500);
  assert.ok(Math.abs((await groStore.get(499)).frame.atoms[0].x-49.9)<1e-10);
  const broken=(await md.open(new File([gro(0),'broken\n2\n'],'x.gro'),'gromacs')).store;
  await assert.rejects(broken.scan(()=>{}),/不完整/);
  await assert.rejects(md.open(new File(['ITEM: UNITS\nlj\n',dump()],'x.dump'),'lammps-dump-text',[{name:'in',text:'dump_modify v element Cu O'}]),/units lj/);
  console.log('MD streams: 500+ frames, bounded cache, units, precision, box origins/tilts, IDs and truncation passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});

// Source metadata survives conversion without treating atom names as force-field types.
{
  const gro='Water\n    1\n    7SOL     OW   42   0.100   0.200   0.300\n   1.00000   1.00000   1.00000\n';
  const converted=md.parseGro(gro);
  const frame=new StructureParser.StructureDocument('metadata.xyz',converted).frame(0);
  assert.equal(frame.atoms[0].name,'OW');assert.equal(frame.atoms[0].sourceId,'42');
  assert.equal(frame.atoms[0].residue,'SOL');assert.equal(frame.atoms[0].type,undefined);
}
