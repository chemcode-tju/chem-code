'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('src/chemistry_agent/web/structure/app.js','utf8');
const elements=new Map(),windowEvents=new Map(),scheduled=new Map();
let sequence=0,playing=false,releaseSeek;
const element=id=>{
  if(!elements.has(id)) {
    const classes=new Set(),attributes=new Map();
    elements.set(id,{value:0,parentElement:{hidden:false},events:new Map(),addEventListener(name,callback){this.events.set(name,callback);},
      getAttribute:name=>attributes.get(name),setAttribute:(name,value)=>attributes.set(name,String(value)),
      classList:{contains:name=>classes.has(name),toggle(name,enabled){enabled?classes.add(name):classes.delete(name);}}});
  }
  return elements.get(id);
};
const doc={count:100,setPlayback(){},prefetch(){}};
const context=vm.createContext({StructureControls:require('../../src/chemistry_agent/web/structure/controls.js'),$:element,doc,frameIndex:2,timer:null,readGeneration:1,
  playbackGeneration:0,timelineSeek:null,resumeWhenVisible:false,viewVisible:true,streamComplete:true,views:null,
  playback:{fps:60,speed:1,step:1,loop:true},performance:{now:()=>0},
  window:{addEventListener:(name,callback)=>windowEvents.set(name,callback)},
  requestAnimationFrame(callback){const id=++sequence;scheduled.set(id,callback);return id;},
  cancelAnimationFrame(id){scheduled.delete(id);},displayPlayback:value=>{playing=value;},updateTimeline(){},
  async go(index){if(releaseSeek)await new Promise(resolve=>{releaseSeek=resolve;});context.frameIndex=Number(index);}
});
vm.runInContext(source.slice(source.indexOf('function stop()'),source.indexOf('function updateTimeline()')),context);
vm.runInContext(source.slice(source.indexOf('function beginTimelineSeek()'),source.indexOf('$("reset").onclick')),context);
const event=name=>element('timeline').events.get(name);

(async()=>{
  element('play').onclick();assert(playing);
  let resized=0;context.views={resizeToLayout(){resized++;}};
  context.setTrajectoryExpanded(true);
  const foldingTimer=context.timer,foldingRun=context.playbackGeneration,foldingFrame=context.frameIndex;
  element('playback-menu').open=true;
  element('viewport-menu').open=true;
  element('trajectory-toggle').onclick();
  assert.equal(element('trajectory-toggle').getAttribute('aria-expanded'),'false');
  assert(element('trajectory-toolbar').classList.contains('trajectory-collapsed'));
  assert.equal(element('playback-menu').open,false,'A folded settings menu must close');
  assert.equal(element('viewport-menu').open,false,'A folded view menu must close');
  assert(playing);assert.equal(context.timer,foldingTimer);assert.equal(context.playbackGeneration,foldingRun);
  assert.equal(context.frameIndex,foldingFrame,'Folding must not reset the active frame');
  element('trajectory-toggle').onclick();
  assert.equal(element('trajectory-toggle').getAttribute('aria-expanded'),'true');
  assert.equal(context.timer,foldingTimer);assert(resized>=3,'The viewport follows the new toolbar height');
  event('pointerdown')();assert.equal(context.timer,null);assert(playing);
  element('timeline').value=52;await event('input')({target:element('timeline')});
  await windowEvents.get('pointerup')();
  assert(playing);assert.notEqual(context.timer,null);assert.equal(context.frameIndex,52);
  // A late duplicate change/blur must not restart or rewind playback.
  const timer=context.timer;await event('change')();await event('blur')();assert.equal(context.timer,timer);
  context.stop();event('pointerdown')();element('timeline').value=21;await event('change')();
  assert(!playing);assert.equal(context.timer,null);assert.equal(context.frameIndex,21);
  context.setTrajectoryExpanded(false);context.setTrajectoryExpanded(true);
  assert(!playing);assert.equal(context.timer,null);assert.equal(context.frameIndex,21,'Paused folds stay paused on the same frame');
  element('play').onclick();element('timeline').value=22;event('input')({target:element('timeline')});await event('change')();
  assert(playing,'Keyboard seeking also preserves playback intent');
  event('pointerdown')();releaseSeek=true;
  const pending=context.finishTimelineSeek();context.stop();releaseSeek();releaseSeek=null;await pending;
  assert(!playing,'Explicit pause while a seek is loading wins');
  element('play').onclick();event('pointerdown')();context.readGeneration++;
  await event('change')();assert.equal(context.timer,null,'Old seek completion must not restart a new document');
  context.stop();
  const frameNumber=element('frame-number');
  context.document={activeElement:frameNumber};
  for(const id of ['trajectory-tools','frame-number']){
    element(id).classList={toggle(){},contains(){return false;}};element(id).closest=()=>({style:{setProperty(){}}});
  }
  // A background index callback must not replace a partly typed frame number.
  vm.runInContext(source.slice(source.indexOf('function updateTimeline()'),source.indexOf('function render(')),context);
  frameNumber.value='75';context.doc.count=500;context.updateTimeline();assert.equal(frameNumber.value,'75');
  frameNumber.blur=()=>{
    // Enter must commit even when the browser does not emit a change on blur.
    context.document.activeElement=null;
    context.updateTimeline();
  };
  frameNumber.events.get('keydown')({key:'Enter',preventDefault(){},target:frameNumber});
  assert.equal(context.frameIndex,74);assert.equal(Number(frameNumber.value),75);

  // Exercise the real display handlers against the running animation loop.
  // They must preserve both playing and paused states, including an async
  // reference-frame read for drift correction while the trajectory advances.
  let repaints=0;
  context.status=()=>{};context.scheduleRender=context.render=()=>{repaints++;};
  context.openPanel=panel=>{panel.open=true;};context.updateSelection=()=>{};
  context.StructureSelection=require('../../src/chemistry_agent/web/structure/selection.js');
  context.StructureGeometry=require('../../src/chemistry_agent/web/structure/geometry.js');
  context.displayState=new context.StructureSelection.DisplayState();
  context.frameData={atoms:[{elem:'O',x:0,y:0,z:0},{elem:'H',x:1,y:0,z:0}]};
  context.selection=[0];
  context.document.createElement=()=>({append(...children){this.children=children;}});
  context.document.createTextNode=text=>({textContent:text});
  element('element-list').replaceChildren=function(...children){this.children=children;};
  element('display-dialog').close=function(){this.open=false;};
  vm.runInContext(source.slice(source.indexOf('function clearFollow()'),source.indexOf('function sceneSettings()')),context);
  vm.runInContext(source.slice(source.indexOf('function resetDisplayState()'),source.indexOf('async function go(')),context);
  vm.runInContext(source.slice(source.indexOf('for (const id of ["style", "palette"'),source.indexOf('function openPanel(')),context);
  vm.runInContext(source.slice(source.indexOf('$("drift-settings").onclick='),source.indexOf('$("source-toggle").onclick')),context);
  const displayIds=['style','palette','radius-scale','cell','labels','hbonds','wrap'];
  const changeDisplay=()=>{
    for(const id of displayIds)element(id).onchange();
    element('display-settings').onclick();assert(element('display-dialog').open);
    const oxygen=element('element-list').children.find(label=>label.children[1].textContent==='O').children[0];
    oxygen.checked=false;oxygen.onchange();assert(context.displayState.hiddenElements.has('O'));
    element('show-elements').onclick();assert.equal(context.displayState.hiddenElements.size,0);
    context.selection=[0];element('selection-style').value='stick';element('apply-display').onclick();
    assert.equal(context.displayState.overrides.get(0).style,'stick');
    element('reset-selected-style').onclick();assert.equal(context.displayState.overrides.size,0);
    element('reset-display').onclick();element('display-close').onclick();
  };
  const advance=async()=>{
    const before=context.frameIndex,callback=scheduled.get(context.timer);
    assert(callback,'Playback still has a scheduled tick');scheduled.delete(context.timer);
    await callback(1000);assert.notEqual(context.frameIndex,before,'Frames continue advancing');
  };
  context.startPlayback();const playbackRun=context.playbackGeneration;
  element('animation-fps').value=10;element('playback-speed').value=2;element('playback-step').value=3;element('playback-loop').checked=true;
  element('playback-step').events.get('input')();
  const beforeSettings=context.frameIndex,settingsTick=scheduled.get(context.timer);scheduled.delete(context.timer);
  await settingsTick(100);assert.equal(context.frameIndex,beforeSettings+6,'New rate, speed and stride apply on the next tick');
  assert.equal(context.playbackGeneration,playbackRun,'Live settings must not restart the loop');
  element('animation-fps').value='';element('animation-fps').events.get('input')();assert.equal(context.playback.fps,10,'Partial numeric edits keep the last valid setting');
  element('animation-fps').value=10;
  changeDisplay();assert(playing);assert.equal(context.playbackGeneration,playbackRun);assert(repaints>displayIds.length);
  await advance();
  element('drift-settings').onclick();assert(element('drift-dialog').open);
  element('drift-reference').value=1;element('drift-source').value='all';element('drift-enabled').checked=true;
  let releaseReference;context.doc.get=()=>new Promise(resolve=>{releaseReference=resolve;});
  const applying=element('drift-apply').onclick();await advance();
  releaseReference({frame:context.frameData});await applying;
  assert.equal(element('drift-error').textContent,'');assert(context.drift);assert.equal(context.drift.reference,0);assert(playing);
  assert.equal(element('drift-atoms').parentElement.hidden,true);
  element('drift-source').value='indices';element('drift-source').onchange();assert.equal(element('drift-atoms').parentElement.hidden,false);
  assert.equal(context.playbackGeneration,playbackRun);
  element('drift-enabled').checked=false;await element('drift-apply').onclick();assert.equal(context.drift,null);
  context.stop();changeDisplay();element('playback-speed').events.get('input')();assert(!playing);assert.equal(context.timer,null);
  console.log('Timeline: playing/paused scrubs, keyboard seek, duplicate completion, explicit pause and stale-file guards passed.');
  console.log('Trajectory disclosure: playing/paused folds preserve frame and animation loop, close settings and resize the viewport.');
  console.log('Display controls: global settings, advanced display, reset and async drift correction preserve playback intent.');
})().catch(error=>{console.error(error);process.exitCode=1;});
