import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const viewer = new URL('../src/chemistry_agent/web/structure/', import.meta.url);

test('one slow animation tick advances only the configured stride', async () => {
  const app = readFileSync(new URL('app.js', viewer), 'utf8');
  const visited = [];
  let tick, now = 0;
  const context = vm.createContext({
    timer:null, viewVisible:true, doc:{count:100}, frameIndex:10,
    streamComplete:true, playbackGeneration:0, playback:{fps:24, speed:1, step:3, loop:true},
    performance:{now:()=>now}, requestAnimationFrame:callback=>{tick=callback;return 1;},
    displayPlayback(){},
    async go(index) {visited.push(index);context.frameIndex=index;},
  });
  vm.runInContext(app.slice(app.indexOf('function startPlayback('), app.indexOf('$("play").onclick')), context);
  context.startPlayback();
  await tick(now=40);
  await tick(now=360);
  await tick(now=425);
  assert.deepEqual(visited, [13, 16, 19]);
});

test('prefetch batches raw neighbors, while a jump can read independently', async () => {
  const parser = readFileSync(new URL('parser.js', viewer), 'utf8');
  const store = readFileSync(new URL('trajectory-store.js', viewer), 'utf8');
  const context = vm.createContext({});
  vm.runInContext(parser, context);
  vm.runInContext(store, context);
  const frame = index => `1\nframe ${index}\nH ${index} 0 0\n`;
  let release;
  const singles = [], batches = [];
  const trajectory = new context.StructureTrajectory.FrameStore('play.xyz', {
    count:40,
    async read(index) {singles.push(index);return frame(index);},
    async readMany(indices, onFrame) {
      batches.push([...indices]);
      await new Promise(resolve=>{release=resolve;});
      for(const index of indices)onFrame(index,frame(index));
    },
  });
  trajectory.put(0,frame(0));
  const batch = trajectory.prefetch(0,2);
  const neighbor = trajectory.get(2);
  const jumped = await trajectory.get(35);
  assert.equal(jumped.frame.atoms[0].x,35);
  release();
  await batch;
  assert.equal((await neighbor).frame.atoms[0].x,2);
  assert.equal(trajectory.cache.get(4).frame,null);
  assert.equal((await trajectory.get(4)).frame.atoms[0].x,4);
  assert.deepEqual(singles,[35]);
  assert.deepEqual(batches,[[2,4,6,8,10,12,14,16]]);
});

test('the index grows even with a full timeline and frame prefetch in flight', async () => {
  const context = vm.createContext({setTimeout});
  vm.runInContext(readFileSync(new URL('trajectory-store.js', viewer), 'utf8'), context);
  let releaseIndex, releaseFrames;
  const source = {
    count:300,
    complete:false,
    scanDelay:0,
    async advance() {
      await new Promise(resolve=>{releaseIndex=resolve;});
      source.count=600;
      source.complete=true;
    },
    async readMany(indices, onFrame) {
      onFrame(indices[0], '1\nfirst\nH 0 0 0\n');
      await new Promise(resolve=>{releaseFrames=resolve;});
    },
  };
  vm.runInContext(readFileSync(new URL('parser.js', viewer), 'utf8'), context);
  const trajectory = new context.StructureTrajectory.FrameStore('xyz.extxyz', source);
  const prefetch = trajectory.prefetch(0,1);
  const progressed = [];
  const scan = trajectory.scan(()=>progressed.push(trajectory.count));
  const first = await trajectory.get(1);
  assert.equal(first.frame.atoms[0].elem,'H');
  assert.ok(trajectory.prefetchTask, 'the first frame is usable while later ones are loading');
  try {
    await new Promise(resolve=>setTimeout(resolve,15));
    assert.equal(typeof releaseIndex, 'function', 'prefetch must not block indexing');
    releaseIndex();
    await scan;
    assert.deepEqual(progressed,[600]);
  } finally {
    trajectory.dispose();
    releaseFrames();
    await prefetch;
  }
});

test('play can start at the first available frame and waits for the selected stride', async () => {
  const app = readFileSync(new URL('app.js', viewer), 'utf8');
  const seen = [];
  let tick, now = 0;
  const context = vm.createContext({
    timer:null, viewVisible:true, doc:{count:1}, frameIndex:0,
    streamComplete:false, playbackGeneration:0, playback:{fps:60, speed:1, step:3, loop:true},
    performance:{now:()=>now}, requestAnimationFrame:callback=>{tick=callback;return 1;},
    displayPlayback(){},
    async go(index) {seen.push(index);context.frameIndex=index;},
  });
  vm.runInContext(app.slice(app.indexOf('function startPlayback('), app.indexOf('$("play").onclick')), context);
  context.startPlayback();
  assert.equal(typeof tick,'function');
  await tick(now=17);
  context.doc.count=3;
  await tick(now=34);
  assert.deepEqual(seen,[]);
  context.doc.count=4;
  await tick(now=51);
  assert.deepEqual(seen,[3]);
});

test('the sequential scan keeps remote blocks for playback and later seeks', async () => {
  const blockSize = 2 * 1024 * 1024;
  const bytes = new Uint8Array(blockSize * 4 + 12);
  for(let block=0;block<5;block++) bytes.fill(65+block,block*blockSize,Math.min(bytes.length,(block+1)*blockSize));
  const calls=[];
  const context = vm.createContext({
    URL, Blob, Promise,
    location:{origin:'http://localhost'},
    async fetch(url) {
      const start=Number(url.searchParams.get('offset'));
      const length=Number(url.searchParams.get('length'));
      assert.equal(url.searchParams.get('snapshot'),calls.length?'v1':null);
      calls.push(start);
      return {
        ok:true, headers:{get:key=>key==='X-Trajectory-Snapshot'?'v1':String(bytes.length)},
        async blob(){return new Blob([bytes.subarray(start,start+length)]);},
      };
    },
  });
  vm.runInContext(readFileSync(new URL('browser-trajectory.js', viewer), 'utf8'), context);
  const adapter=vm.runInContext("StructureBrowserTrajectory.open({name:'XDATCAR.bak',url:'/api/file-browser/trajectory'})",context);
  const file=await adapter.initialize();
  assert.equal(await file.slice(0,7).text(),'AAAAAAA');
  // Two reads of the same aligned block share the same in-flight request.
  await Promise.all([file.slice(100000,100008).text(),file.slice(100004,100012).text()]);
  assert.equal(calls.filter(start=>start===0).length,2);
  for(let start=blockSize;start<bytes.length;start+=blockSize)await file.slice(start,start+8).text();
  const afterScan=calls.length;
  assert.equal(await file.slice(blockSize-3,blockSize+4).text(),'AAABBBB');
  assert.equal(await file.slice(bytes.length-5,bytes.length).text(),'EEEEE');
  assert.equal(calls.length,afterScan);
});

test('explicit 5× speed samples five trajectory steps per tick at a 60 fps target', async () => {
  const app=readFileSync(new URL('app.js', viewer),'utf8');
  const visited=[];
  let tick, now=0;
  const context=vm.createContext({
    timer:null, viewVisible:true, doc:{count:100}, frameIndex:0,
    streamComplete:true, playbackGeneration:0, playback:{fps:60,speed:5,step:2,loop:true},
    performance:{now:()=>now}, requestAnimationFrame:callback=>{tick=callback;return 1;},
    displayPlayback(){},
    async go(index){visited.push(index);context.frameIndex=index;},
  });
  vm.runInContext(app.slice(app.indexOf('function startPlayback('),app.indexOf('$("play").onclick')),context);
  context.startPlayback();
  await tick(now=17);
  await tick(now=34);
  await tick(now=51);
  assert.deepEqual(visited,[10,20,30]);
});
