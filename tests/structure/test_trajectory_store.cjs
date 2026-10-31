const assert=require('node:assert/strict');
global.StructureParser=require('../../src/chemistry_agent/web/structure/parser.js');
global.ASETrajectory=require('../../src/chemistry_agent/web/structure/ase-traj.js');
const {local,host,FrameStore}=require('../../src/chemistry_agent/web/structure/trajectory-store.js');
const frame=i=>`1\r\n中文 ${i}\r\nH ${i} 0 0\r\n`;
async function main(){
  const blob=new Blob(Array.from({length:120},(_,i)=>frame(i)));
  const store=await local(blob,'sample.extxyz',{maxFrames:3});
  assert.equal(store.count,1);assert.equal(store.complete,false);
  assert.equal((await store.get(0)).frame.atoms[0].x,0);
  await store.scan(()=>{});assert.equal(store.count,120);
  for(const i of [119,48,6,95,0])assert.equal((await store.get(i)).frame.atoms[0].x,i);
  assert.equal(store.cache.size,3);assert(!store.cache.has(48));
  assert.equal((await store.get(48)).text,frame(48)); // exact refetch after eviction
  const limited=await local(blob,'sample.xyz',{maxBytes:1});
  await limited.scan(()=>{});await limited.get(8);assert.equal(limited.cache.size,1);
  const broken=await local(new Blob([frame(0),'broken trailing frame']),'x.xyz');
  assert.equal(broken.count,1);await assert.rejects(broken.scan(()=>{}));
  assert.equal((await broken.get(0)).frame.atoms.length,1);
  for(const name of ['POSCAR','CONTCAR','XDATCAR']) {
    const header='Cu O\n1\n10 0 0\n0 10 0\n0 0 15\nCu O\n1 1\n';
    const file=new Blob([header+(name==='XDATCAR'?'Direct configuration= 1\n0 0 0\n.1 .2 .3\nDirect configuration= 2\n.1 0 0\n.1 .2 .4':'Selective dynamics\nCartesian\n0 0 0 T T T\n1 2 3 F F F')]);
    const parsed=new StructureParser.StructureDocument(name,await file.text());
    const indexed=await local(file,name,{maxFrames:1});await indexed.scan(()=>{});
    assert.equal(indexed.count,parsed.count);
    for(let i=indexed.count-1;i>=0;i--)assert.deepEqual((await indexed.get(i)).frame,parsed.frame(i));
  }
  const boundary=await local(new Blob(['1\n'+'a'.repeat(65532)+'中文\nH 2 3 4\n',frame(1)]),'boundary.xyz');
  await boundary.scan(()=>{});assert.equal((await boundary.get(1)).frame.atoms[0].x,1);
  let reads=0;
  const fake=new FrameStore('x.xyz',{count:2,async read(i){reads++;await new Promise(r=>setTimeout(r,1));return frame(i);}}, {maxFrames:1});
  const [a,b]=await Promise.all([fake.get(0),fake.get(0)]);assert.equal(a,b);assert.equal(reads,1);
  fake.dispose();await assert.rejects(fake.get(0),/关闭/);
  const calls=[];
  const remote=host('x.extxyz',async args=>{
    calls.push(args);
    if(args.indexOnly)return {offsets:[100,200],nextOffset:300,eof:true};
    return {text:frame(args.offset/100)};
  },{offsets:[0],text:frame(0),nextOffset:100,version:'v1',eof:false},{maxFrames:1});
  assert.equal(calls.length,0);await remote.scan(()=>{});
  assert.equal(remote.count,3);await remote.get(2);await remote.get(0);
  assert.equal(calls[0].indexOnly,true);
  assert.deepEqual(calls[2],{offset:0,frameLimit:1,version:'v1'});
  const binary=host('x.traj',async args=>({text:frame(args.offset)}),{offsets:[0],text:frame(0),nextOffset:1,totalFrames:800,eof:false});
  assert.equal(binary.complete,true);assert.equal(binary.count,800);
  assert.equal((await binary.get(799)).frame.atoms[0].x,799);
  console.log('Frame store: early first frame, byte offsets, bounded LRU, eviction/refetch, deduplication, VASP, binary logical offsets and partial-tail retention passed.');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
