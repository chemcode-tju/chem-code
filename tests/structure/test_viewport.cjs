const assert = require('node:assert/strict');
const {projectToView, viewQuaternions} = require('../../src/chemistry_agent/web/structure/viewport.js');
const {radius, palette, vesta} = require('../../src/chemistry_agent/web/structure/appearance.js');
function close(actual, expected) { actual.forEach((n,i) => assert.ok(Math.abs(n-expected[i]) < 1e-10)); }
close(projectToView([1,0,0], viewQuaternions.xz), [1,0,0]);
close(projectToView([0,0,1], viewQuaternions.xz), [0,1,0]);
close(projectToView([0,1,0], viewQuaternions.yz), [-1,0,0]);
close(projectToView([0,0,1], viewQuaternions.yz), [0,1,0]);
// The viewing axis points towards the camera (+screen Z). Keep world Z
// upright in side views and world Y upright in top/bottom views.
const {axisShortcut}=require('../../src/chemistry_agent/web/structure/viewport.js');
for(const [i,key] of ['KeyX','KeyY','KeyZ'].entries())for(const reverse of [false,true]){
  const preset=axisShortcut({code:key,altKey:reverse,key:reverse?'≈':key.slice(-1)});
  const eye=[0,0,0];eye[i]=reverse?-1:1;
  close(projectToView(eye,viewQuaternions[preset]),[0,0,1]);
  close(projectToView(i===2?[0,1,0]:[0,0,1],viewQuaternions[preset]),[0,1,0]);
}
for(const ignored of [{metaKey:true},{ctrlKey:true},{isComposing:true},{repeat:true},{target:{closest:()=>true}}])
  assert.equal(axisShortcut({code:'KeyX',...ignored}),undefined);
const frame = [10,10,45];
close(projectToView(frame, viewQuaternions.yz), [-10,45,-10]);
assert.equal(radius('H'), .46);
assert.equal(radius('Cu'), 1.28);
assert.equal(radius('O'), .74);
assert.equal(radius('unknown'), 1.2);
assert.ok(Number.isInteger(vesta.Cu));
assert.deepEqual(palette('rasmol', {Jmol:{H:1,Cu:2,Og:3}, rasmol:{H:4}}), {H:4,Cu:2,Og:3});
console.log('View geometry: XZ/YZ axes and tall-cell projection; OVITO display radii and palette fallback passed.');

// Selection must preserve the existing model/camera; mock only the renderer boundary.
const {StructureViews}=require('../../src/chemistry_agent/web/structure/viewport.js');
global.StructureSelection=require('../../src/chemistry_agent/web/structure/selection.js');
global.StructureAppearance=require('../../src/chemistry_agent/web/structure/appearance.js');
const model={},camera=[1,2,3,4,0,0,0,1],removed=[];
const viewer={getView:()=>camera.slice(),removeShape:s=>removed.push(s.shapePosition),addLine:s=>({...s,shapePosition:9}),render:()=>{},model};
const pane={element:{hidden:false},viewer,markerShapes:[{shapePosition:3}],measureShapes:[{shapePosition:2}]};
const views=Object.create(StructureViews.prototype);
const atoms=[{elem:'H',x:0,y:0,z:0},{elem:'O',x:1,y:0,z:0}];
Object.assign(views,{latest:[{atoms},{scale:1},[]],panes:[pane],visibleIndices:[0,1],rules:[{style:'sphere'},{style:'sphere'}]});
views.updateSelection([0,1]);
assert.deepEqual(removed,[3,2]);assert.equal(pane.markers.length,2);assert.equal(pane.measureShapes.length,1);
assert.equal(viewer.model,model);assert.deepEqual(viewer.getView(),camera);
views.updateSelection([]);assert.equal(pane.markers.length,0);assert.equal(pane.measureShapes.length,0);
console.log('Selection updates preserve the atom model and camera, including clear selection.');

// Exercise actual pointer handlers and ResizeObserver without a GPU.
{
 const handlers={},translations=[],resizes=[],paints=[];
 const queued=new Map(),timers=new Map();let ticket=0;
 global.requestAnimationFrame=callback=>{queued.set(++ticket,callback);return ticket;};
 global.cancelAnimationFrame=id=>queued.delete(id);
 global.setTimeout=callback=>{timers.set(++ticket,callback);return ticket;};
 global.clearTimeout=id=>timers.delete(id);
 const flush=()=>{const batch=[...queued.values()];queued.clear();batch.forEach(fn=>fn());};
 const settle=()=>{const batch=[...timers.values()];timers.clear();batch.forEach(fn=>fn());flush();flush();};
 const canvas={clientWidth:400,clientHeight:600};
 const layer={querySelector:()=>({hidden:true,style:{}}),addEventListener:()=>{},setPointerCapture:()=>{},getBoundingClientRect:()=>({left:0,top:0})};
 const title={};
 const element={hidden:false,focus(){},querySelector:selector=>selector==='.view-canvas'?canvas:selector==='.pointer-layer'?layer:title,
   addEventListener:(name,fn)=>{(handlers[name]??=[]).push(fn);},setPointerCapture:()=>{}};
 let resizeCallback;
 const fakeViewer={setViewChangeCallback:()=>{},resize:()=>resizes.push([canvas.clientWidth,canvas.clientHeight]),render:()=>paints.push(1),translateScene:(dx,dy)=>translations.push([dx,dy])};
 global.document={createElement:tag=>tag==='section'?element:{},querySelector:()=>({label:'Front · XZ'})};
 global.$3Dmol={createViewer:()=>fakeViewer};
 global.ResizeObserver=class {constructor(callback){resizeCallback=callback;}observe(){}};
 const interaction=Object.create(StructureViews.prototype);
 Object.assign(interaction,{container:{append:()=>{}},panes:[],activate:()=>{},fit:()=>{throw Error('Unexpected refit');}});
 interaction.createPane('xz',0);
 const emit=(name,properties)=>{const event={target:{closest:()=>null},preventDefault(){},stopImmediatePropagation(){this.stopped=true;},...properties};for(const handler of handlers[name]){handler(event);if(event.stopped)break;}};
 emit('pointerdown',{button:0,pointerId:1,clientX:10,clientY:20});
 emit('pointermove',{pointerId:1,clientX:30,clientY:40});assert.deepEqual(translations,[]);
 emit('pointerdown',{button:2,pointerId:2,clientX:10,clientY:20});
 emit('pointermove',{pointerId:2,clientX:30,clientY:15});assert.deepEqual(translations,[[20,-5]]);
 emit('pointerup',{pointerId:2});emit('pointermove',{pointerId:2,clientX:40,clientY:20});assert.equal(translations.length,1);
 resizeCallback();resizeCallback();resizeCallback();assert.equal(timers.size,1);assert.equal(resizes.length,0);settle();
 assert.deepEqual(resizes,[[400,600]]);assert.equal(paints.length,0);
 resizeCallback();settle();assert.equal(resizes.length,1);
 canvas.clientWidth=300;resizeCallback();settle();assert.deepEqual(resizes[1],[300,600]);
 element.hidden=true;canvas.clientWidth=0;resizeCallback();settle();assert.equal(resizes.length,2);
 element.hidden=false;canvas.clientWidth=450;resizeCallback();settle();assert.deepEqual(resizes[2],[450,600]);
 // During a resize burst the previous frame stays visible until two paint
 // opportunities after the final resize, including when input flushes it early.
 let copies=0,removals=0;
 canvas.querySelector=()=>({width:900,height:1200});
 element.append=()=>{};
 global.document.createElement=()=>({style:{},getContext:()=>({drawImage:()=>copies++}),remove:()=>removals++});
 const pane=interaction.panes[0];pane.ready=true;
 canvas.clientWidth=460;resizeCallback();
 canvas.clientWidth=470;resizeCallback();
 assert.equal(copies,1);assert.ok(pane.resizeCover);assert.equal(resizes.length,3);
 emit('pointerdown',{button:0,pointerId:3,clientX:100,clientY:100});
 assert.deepEqual(resizes.at(-1),[470,600]);assert.ok(pane.resizeCover);
 flush();assert.ok(pane.resizeCover);flush();assert.equal(pane.resizeCover,null);assert.equal(removals,1);
 canvas.clientWidth=480;resizeCallback();settle();assert.equal(copies,2);assert.equal(removals,2);
 // Footer expansion changes height in one step, so the canvas must already
 // match before the next paint, with no delayed white cover or extra redraw.
 canvas.clientHeight=540;interaction.resizeToLayout();
 assert.deepEqual(resizes.at(-1),[480,540]);assert.equal(pane.resizeCover,null);
 const afterFooter=resizes.length;resizeCallback();settle();
 assert.equal(resizes.length,afterFooter);assert.equal(copies,2);
 // Collapsing during a sidebar drag must also cancel its pending snapshot.
 canvas.clientWidth=490;resizeCallback();assert.ok(pane.resizeCover);
 canvas.clientHeight=600;interaction.resizeToLayout();
 assert.deepEqual(resizes.at(-1),[490,600]);assert.equal(pane.resizeCover,null);
 assert.equal(timers.size,0);assert.equal(removals,3);
 const afterInterrupt=resizes.length;settle();assert.equal(resizes.length,afterInterrupt);
 interaction.mode='box';let boxes=0;interaction.onBox=()=>boxes++;
 const mouse={button:0,pointerId:1,clientX:100,clientY:100,preventDefault(){}};
 layer.onpointerdown(mouse);layer.onpointerup(mouse);assert.equal(boxes,0);
 layer.onpointerdown(mouse);layer.onpointermove({...mouse,clientX:102});layer.onpointerup({...mouse,clientX:102});assert.equal(boxes,0);
 layer.onpointerdown(mouse);layer.onpointercancel();layer.onpointerup(mouse);assert.equal(boxes,0);
 console.log('Sidebar resize settles once; footer toggles paint immediately and clear stale covers without resetting the view.');
}

// Shift bypasses rotation/zoom, ray-picks the front atom, and toggles a dragged
// rectangle without changing the persistent tool or losing selection order.
{
 const handlers={},rectangle={hidden:true,style:{}},picks=[],boxes=[];
 const element={querySelector:()=>rectangle,setPointerCapture:()=>{},addEventListener:(name,fn)=>{(handlers[name]??=[]).push(fn);}};
 const view=Object.create(StructureViews.prototype);
 let hits=[{clickable:{index:1}}];
 const canvas={getBoundingClientRect:()=>({left:20,top:40,width:400,height:600})};
 const viewer={targetedObjects:(x,y)=>{assert.equal(x,0);assert.equal(y,0);return hits;},modelToScreen:a=>({x:a.x,y:a.y})};
 Object.assign(view,{mode:'rotate',visibleIndices:[0,2],latest:[{atoms:[{x:80,y:80},{x:85,y:85},{x:100,y:100}]}],onAtom:(...args)=>picks.push(args),onBox:(...args)=>boxes.push(args)});
 global.scrollX=global.scrollY=0;
 view.bindShiftSelection({element,canvas,viewer});
 const emit=(name,props={})=>{const e={button:0,shiftKey:true,pointerId:1,clientX:220,clientY:340,target:{closest:()=>null},preventDefault(){this.prevented=true;},stopImmediatePropagation(){this.stopped=true;},...props};for(const handler of handlers[name]||[])handler(e);return e;};
 assert.equal(emit('pointerdown').stopped,true);assert.equal(emit('mousedown').stopped,true);
 emit('pointerup');assert.deepEqual(picks,[[2,true]]);assert.equal(view.mode,'rotate');
 emit('pointerdown');emit('pointermove',{clientX:222,clientY:341});emit('pointerup');assert.equal(picks.length,2);assert.equal(boxes.length,0);
 emit('pointerdown',{clientX:150,clientY:150});emit('pointermove',{clientX:50,clientY:50});assert.equal(rectangle.hidden,false);
 emit('pointerup',{clientX:50,clientY:50,shiftKey:false});assert.deepEqual(boxes,[[[0,2],true]]);assert.equal(rectangle.hidden,true);
 emit('pointerdown');emit('pointercancel');emit('pointerup');assert.equal(picks.length,2);
 hits=[];emit('pointerdown');emit('pointerup');assert.equal(picks.length,2);
 assert.equal(emit('pointerdown',{shiftKey:false}).stopped,undefined);
 assert.equal(emit('pointerdown',{button:2}).stopped,undefined);
 console.log('Shift click/front-atom picking, drag toggle, hidden-atom exclusion, jitter and cancellation passed.');
}

{
 const {labelScreenScale}=require('../../src/chemistry_agent/web/structure/viewport.js');
 assert.equal(labelScreenScale(100,14,8),1); // normal close-up capped at 14px
 assert.equal(labelScreenScale(1000,14,8),1);
 const far=labelScreenScale(4,14,8);
 assert.ok(far*14<=4);assert.ok(far>0);
 assert.ok(labelScreenScale(.01,14,8)<.001); // no forced minimum
 assert.ok(labelScreenScale(10,14,25)*25<=8.5); // multi-digit IDs fit the atom
 assert.equal(labelScreenScale(40,14,25),labelScreenScale(250,87.5,156.25)); // 600 DPI export
 console.log('Labels scale with projected atom diameter, cap at 14px, and preserve export scaling.');
}

// A restored GPU context recreates label materials without moving the camera.
// Its reset scale must be corrected even if the view cache still matches.
{
 const scales=[];let paints=0;
 global.$3Dmol={Matrix4:class {getInverse(){return {elements:[1,0,0,0,0,1,0,0,0,0,1]};}}};
 const atom={x:0,y:0,z:0};
 const label={sprite:{material:{},position:{set(){}},scale:{set:(x,y)=>scales.push([x,y])}}};
 const renderer={getView:()=>[0,0,0,0,0,0,0,1],modelGroup:{matrixWorld:{}},camera:{position:{clone:()=>({applyMatrix4:()=>({x:0,y:0,z:10})})}},
   modelToScreen:()=>({x:50,y:50}),screenToModelDistance:()=>.1,render:()=>paints++};
 const pane={viewer:renderer,preset:'xz',canvas:{clientWidth:100,clientHeight:100},atomLabels:[{atom,radius:1,label,sampling:3,fontSize:14,textWidth:8}]};
 const views=Object.create(StructureViews.prototype);
 views.drawSelection(pane);assert.equal(scales.length,1);
 views.drawSelection(pane);assert.equal(scales.length,1);
 label.sprite.material={};views.drawSelection(pane);
 assert.equal(scales.length,2);assert.deepEqual(scales[0],scales[1]);assert.equal(paints,2);
 console.log('Context recovery reapplies label scales without requiring a camera change.');
}

// Check both 3Dmol projection conventions while width and height vary
// independently. Repeated resize must not accumulate any change in scale.
for(const preset of ['xz','perspective']){
 const viewer={fov:20,setCameraParameters({fov}){this.fov=fov;},resize(){}};
 const pane={viewer,preset,projectionSize:{width:650,height:800}};
 const focalLength=(width,height)=>preset==='xz'
   ?width/(2*Math.tan(viewer.fov*Math.PI/180))
   :height/(2*Math.tan(viewer.fov*Math.PI/360));
 const initial=focalLength(650,800);
 for(const [w,h] of [[490,800],[810,575],[340,290],[650,800]]){
   StructureViews.prototype.resizePane(pane,w,h,`${w}:${h}:2`);
   assert.ok(Math.abs(focalLength(w,h)-initial)<1e-9);
 }
}
