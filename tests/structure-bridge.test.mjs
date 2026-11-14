import test from 'node:test';
import assert from 'node:assert/strict';
import {createStructureBridge} from '../src/chemistry_agent/web/structure-links.mjs';

function fixture() {
  const sent=[],frames=[],errors=[],listeners=[];
  globalThis.location={origin:'http://localhost:8768'};
  globalThis.window={addEventListener:(name,callback)=>listeners.push(callback)};
  const target={postMessage:(data,origin)=>sent.push({data,origin})};
  let src=true;
  const frame={contentWindow:target,hasAttribute:()=>src,removeAttribute:()=>{src=false;}};
  const bridge=createStructureBridge({frame,selectTab:()=>{},toast:text=>errors.push(text),onFrame:result=>frames.push(result)});
  const emit=(data,{origin=location.origin,source=target}={})=>listeners.forEach(callback=>callback({data,origin,source}));
  return {bridge,sent,frames,errors,emit};
}

test('file and ionic selection travel together after viewer readiness',async()=>{
  const f=fixture();
  await f.bridge.open('XDATCAR',async()=>new Blob(['trajectory']),{selection:{ionicStep:98,stride:1}});
  assert.equal(f.sent.length,0);
  f.emit({type:'chem:viewer-ready'});
  assert.equal(f.sent.length,1);const request=f.sent[0].data;
  assert.equal(request.file.name,'XDATCAR');assert.equal(request.selection.ionicStep,98);
  f.emit({type:'chem:viewer-loaded',requestId:request.requestId,frameIndex:97,ionicStep:98});
  assert.equal(f.frames.at(-1).frameIndex,97);
  f.bridge.seek({ionicStep:42});
  assert.equal(f.sent.at(-1).data.type,'chem:seek-structure');
  assert.equal(f.sent.at(-1).data.selection.ionicStep,42);
  assert.equal(f.sent.filter(r=>r.data.type==='chem:open-structure').length,1);
  f.bridge.reset();
});
test('latest pending seek wins and stale acknowledgments cannot change the selected frame',async()=>{
  const f=fixture();f.emit({type:'chem:viewer-ready'});
  await f.bridge.open('XDATCAR',async()=>new Blob(['file']),{selection:{ionicStep:2}});
  const request=f.sent[0].data;
  f.bridge.seek({ionicStep:80});f.bridge.seek({ionicStep:81});
  f.emit({type:'chem:viewer-loaded',requestId:request.requestId,frameIndex:1});
  const seek=f.sent.at(-1).data;assert.equal(seek.selection.ionicStep,81);
  assert.equal(f.frames.length,0);
  f.emit({type:'chem:viewer-frame',requestId:request.requestId,navigationId:seek.navigationId-1,frameIndex:79});
  assert.equal(f.frames.length,0);
  f.emit({type:'chem:viewer-frame',requestId:request.requestId,navigationId:seek.navigationId,frameIndex:80});
  assert.equal(f.frames.at(-1).frameIndex,80);f.bridge.reset();
});
test('another origin, another iframe and a superseded file cannot control the viewer',async()=>{
  const f=fixture();let resolve;
  const pending=f.bridge.open('old',()=>new Promise(done=>{resolve=done;}));
  f.emit({type:'chem:viewer-ready'},{origin:'https://other.test'});
  f.emit({type:'chem:viewer-ready'},{source:{}});
  f.bridge.reset();resolve(new Blob(['old']));await pending;
  assert.equal(f.sent.length,0);
  f.emit({type:'chem:viewer-loaded',requestId:1,frameIndex:99});
  assert.equal(f.frames.length,0);
});

test('an unsuccessful initial frame lookup can be retried instead of waiting forever',async()=>{
  const f=fixture();f.emit({type:'chem:viewer-ready'});
  await f.bridge.open('XDATCAR',async()=>new Blob(['file']),{selection:{ionicStep:999}});
  f.emit({type:'chem:viewer-error',requestId:f.sent[0].data.requestId,message:'尚无该帧'});
  assert.equal(f.bridge.seek({ionicStep:98}),false);
  await f.bridge.open('XDATCAR',async()=>new Blob(['file']),{selection:{ionicStep:98}});
  assert.equal(f.sent.at(-1).data.selection.ionicStep,98);f.bridge.reset();
});

test('LAMMPS trajectory keeps its own metadata and real timestep across the viewer bridge',async()=>{
  const f=fixture();f.emit({type:'chem:viewer-ready'});
  await f.bridge.open('md.dump',async()=>new Blob(['dump']),{selection:{simulationStep:0},companions:[{name:'in.lammps',text:'units metal\nmass 1 63.546'}]});
  const request=f.sent[0].data;assert.equal(request.selection.simulationStep,0);
  assert.equal(request.companions[0].name,'in.lammps');assert.match(await request.companions[0].text(),/units metal/);
  f.bridge.reset();
});

test('ordered path sequence is loaded once and image changes only seek frames',async()=>{
  const f=fixture();f.emit({type:'chem:viewer-ready'});
  const files=[new File(['first'],'image_00.vasp'),new File(['last'],'image_01.vasp')];
  await f.bridge.open('NEB path',async()=>files,{sequence:true,selection:{frameIndex:0}});
  const message=f.sent[0].data;
  assert.equal(message.sequence,true);assert.equal(message.files.length,2);
  assert.equal(await message.files[1].text(),'last');
  f.emit({type:'chem:viewer-loaded',requestId:message.requestId,frameIndex:0,frameCount:2});
  f.bridge.seek({frameIndex:1});
  assert.equal(f.sent.at(-1).data.type,'chem:seek-structure');
  f.emit({type:'chem:viewer-position',requestId:message.requestId,frameIndex:1,frameCount:2});
  assert.equal(f.frames.at(-1).frameIndex,1);
  assert.equal(f.sent.filter(item=>item.data.type==='chem:open-structure').length,1);
  f.bridge.reset();
});
