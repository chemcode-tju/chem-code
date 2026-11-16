"use strict";
const $ = id => document.getElementById(id);
let doc, frameIndex = 0, frameData, views;
let selection = [], timer = null, loading = false, readGeneration = 0;
let displayState = new StructureSelection.DisplayState();
let streamComplete = true;
let drift = null, paintTicket = 0, snapshotTicket = 0, snapshotData = null;
let activeRecord = null;
let navigationTarget = null, navigationTask = null;
let playbackGeneration = 0;
let timelineSeek=null,viewVisible=true,resumeWhenVisible=false,tickLayout='';
let inspector;
let playback = {fps:60, speed:1, step:1, loop:true};
function afterPaint(callback) { requestAnimationFrame(() => setTimeout(callback, 0)); }
function scheduleRender(reset = false) { const ticket=++paintTicket; afterPaint(() => { if(ticket===paintTicket)render(reset); }); }
function clearFollow() { drift=null;$("drift-enabled").checked=false;$("drift-state").textContent="";$("drift-state").hidden=true; }
function sceneSettings() {
  return StructureScene.settings(frameData,{style:$("style").value,palette:$("palette").value,
    scale:Number($("radius-scale").value),cell:$("cell").checked,labels:$("labels").checked,
    hbonds:$("hbonds").checked,wrap:$("wrap").checked,drift,display:displayState});
}
function displayedFrame() {return StructureScene.transform(frameData,{drift,wrap:$("wrap").checked});}
function status(text, error = false) { $("status").textContent = error ? text : ""; $("status").hidden = !error; }
function displayPlayback(playing) {
  const button = $("play"), label = playing ? "暂停轨迹" : "播放轨迹";
  button.querySelector("use").setAttribute("href", "/assets/ui-icons.svg#i-" + (playing ? "pause" : "play"));
  button.setAttribute("aria-label", label);
  button.title = label;
}
function stop() { ++playbackGeneration; cancelAnimationFrame(timer); timer = null; timelineSeek=null;resumeWhenVisible=false;doc?.setPlayback?.(frameIndex,false);displayPlayback(false); }
function setTrajectoryExpanded(expanded) {
  const button = $("trajectory-toggle"), label = expanded ? "收起轨迹工具" : "展开轨迹工具";
  $("trajectory-toolbar").classList.toggle("trajectory-collapsed", !expanded);
  button.setAttribute("aria-expanded", String(expanded));
  button.setAttribute("aria-label", label); button.title = label;
  if (!expanded) for (const id of ["playback-menu", "viewport-menu"]) $(id).open = false;
  // Folding only changes chrome: the active frame and playback loop stay intact.
  views?.resizeToLayout();
  updateTimeline();
}
$("trajectory-toggle").onclick = () => setTrajectoryExpanded($("trajectory-toggle").getAttribute("aria-expanded") !== "true");
function updateTimeline() {
  if (!doc) return;
  $("timeline").max = doc.count - 1; if(!timelineSeek)$("timeline").value = frameIndex; $("timeline").disabled = doc.count < 2;
  $("frame-number").max = doc.count;
  if(document.activeElement!==$("frame-number"))$("frame-number").value = frameIndex + 1;
  const total = String(doc.count) + (streamComplete ? "" : "+");
  $("frame-total").textContent = total;
  const single = streamComplete && doc.count === 1;
  const changed=$("trajectory-tools").classList.contains("single-frame")!==single;
  $("trajectory-tools").classList.toggle("single-frame", single);
  if(changed)views?.resizeToLayout();
  $("frame-number").readOnly = single;
  $("first").disabled = $("previous").disabled = frameIndex === 0;
  $("next").disabled = $("last").disabled = frameIndex === doc.count - 1;
  $("play").disabled = doc.count < 2;
  $("frame-number").closest(".frame-label").style.setProperty("--frame-digits", Math.max(2, total.length) + "ch");
  if (single) return;
  const width=$("ticks").clientWidth;
  if(!width)return;
  const font=getComputedStyle($("ticks")).font,key=JSON.stringify([doc.count,width,font]);
  if(tickLayout===key)return;tickLayout=key;
  const canvas=document.createElement('canvas'),context=canvas.getContext('2d');
  context.font=font;
  const marks=StructureControls.timelineTicks(doc.count,width,text=>context.measureText(text).width);
  $("ticks").replaceChildren(...marks.map(({value,x,tickX})=>{
    const span=document.createElement('span');span.textContent=value;span.style.left=x+'px';span.style.setProperty('--tick-offset',(tickX-x)+'px');return span;
  }));
}
function render(reset = false, strict = false) {
  if (!doc) return;
  try {
    if(!activeRecord)return;
    frameData = activeRecord.frame;
    if (!views) views = new StructureViews($("viewports"), chooseAtom, chooseBox);
    selection = selection.filter(i => i < frameData.atoms.length);
    views.renderScene(StructureScene.build(frameData,sceneSettings()),selection,reset);
    updateTimeline();
    if (!$("source-panel").hidden) $("source").value = activeRecord.text;
    $("source-title").textContent = doc.vaspPreview ? `第 ${frameIndex + 1} 帧 VASP 源码` : doc.xyz ? `第 ${frameIndex + 1} 帧源码` : "VASP 文件源码";
    updateSelection();
    if(inspector)inspector.transformed=Boolean(drift || $("wrap").checked);
    inspector?.update(frameData,frameIndex,selection);
  } catch (error) { stop(); status(error.message, true); if(strict)throw error; }
}
function updateSelection() {
  const count = selection.length;
  $("atom-panel").hidden = count === 0;
  $("atom-title").textContent=selection.length?`已选 ${selection.length}`:'';
  if(!frameData)return;
  const atoms=displayedFrame().atoms;
  $("measurement").textContent=selection.length>4?StructureSelection.formula(atoms,selection):StructureSelection.measure(atoms,selection);
  $("atom-list").textContent=selection.length===1?(()=>{const i=selection[0],a=atoms[i];return `${a.elem} #${i+1}  (${a.x.toFixed(4)}, ${a.y.toFixed(4)}, ${a.z.toFixed(4)}) Å`;})():selection.length<=4?selection.map(i=>`${atoms[i].elem} #${i+1}`).join(' → '):'';
  $("clear-selection").disabled=!selection.length;
  $("selection-count").textContent=`选中原子 · ${selection.length}`;
  $("apply-display").disabled=$("reset-selected-style").disabled=!selection.length;
}
function selectionChanged() {
  stop();
  updateSelection();
  inspector?.update(frameData,frameIndex,selection,true);
  const ticket=++paintTicket;
  afterPaint(()=>{if(ticket===paintTicket)views?.updateSelection(selection);});
}
function chooseAtom(index, shortcut = false) {
  if(!shortcut&&views?.mode!=="select")return;
  selection=StructureSelection.toggleIndices(selection,[index]);selectionChanged();
}
function chooseBox(indices, toggle) {
  if(!indices.length)return;
  selection=toggle?StructureSelection.toggleIndices(selection,indices):indices;selectionChanged();
}
function resetDisplayState() {
  displayState = new StructureSelection.DisplayState(); selection = [];
  $("atom-panel").hidden = true;
}
function populateElements() {
  if(!frameData)return;
  $("element-list").replaceChildren(...[...new Set(frameData.atoms.map(a=>a.elem))].sort().map(elem=>{
    const label=document.createElement('label'),input=document.createElement('input');input.type='checkbox';input.checked=!displayState.hiddenElements.has(elem);
    input.onchange=()=>{if(input.checked)displayState.hiddenElements.delete(elem);else displayState.hiddenElements.add(elem);scheduleRender();};
    label.append(input,document.createTextNode(elem));return label;
  }));
  updateSelection();
}
$("clear-selection").onclick=()=>{selection=[];selectionChanged();};
$("display-settings").onclick=()=>{populateElements();openPanel($("display-dialog"));};
$("display-close").onclick=()=>$("display-dialog").close();
$("show-elements").onclick=()=>{displayState.hiddenElements.clear();populateElements();scheduleRender();};
$("apply-display").onclick=()=>{
  const style=$("selection-style").value,palette=$("selection-palette").value,visible=$("selection-visible").value;
  displayState.apply(frameData,selection,{...(style?{style}:{}),...(palette?{palette,color:palette==='custom'?$("selection-color").value:null}:{}),...(visible?{visible:visible==='show'}:{})});scheduleRender();
};
$("reset-selected-style").onclick=()=>{displayState.reset(frameData,selection);scheduleRender();};
$("reset-display").onclick=()=>{resetDisplayState();populateElements();scheduleRender();};
async function go(index) {
  if(!doc)return;
  navigationTarget=Math.max(0,Math.min(doc.count-1,Number(index)||0));
  if(navigationTask)return navigationTask;
  const currentDoc=doc,generation=readGeneration;
  navigationTask=(async()=>{
    try {
      while(navigationTarget!==null&&doc===currentDoc&&generation===readGeneration) {
        const target=navigationTarget;navigationTarget=null;
        const record=await currentDoc.get(target);
        if(doc!==currentDoc||generation!==readGeneration)return;
        if(navigationTarget!==null&&navigationTarget!==target)continue;
        selection=StructureSelection.remap(frameData,record.frame,selection);
        displayState.transition(frameData,record.frame);
        activeRecord=record;frameData=record.frame;frameIndex=target;status("");
        let driftStopped=false;
        if(drift&&StructureSelection.remap(drift.referenceFrame,frameData,drift.indices).length!==drift.indices.length){clearFollow();driftStopped=true;}
        render();
        window.dispatchEvent(new CustomEvent('structure-frame-change',{detail:{generation,frameIndex,frameCount:doc.count,ionicStep:doc.source.ionicSteps?.[frameIndex] ?? null,simulationStep:doc.source.simulationSteps?.[frameIndex] ?? null}}));
        if(driftStopped)status('当前帧的原子无法与参考帧对应，已停用质心校正',true);
      }
    }catch(error){if(doc===currentDoc){stop();status(error.message,true);}}
    finally{if(generation===readGeneration)navigationTask=null;}
  })();
  return navigationTask;
}
function setExportAvailable(available) {
  $("snapshot").disabled=!available;
  const trigger=$("export-menu").querySelector("summary");
  trigger.setAttribute("aria-disabled",String(!available));trigger.tabIndex=available?0:-1;
  if(!available)$("export-menu").open=false;
}
$("export-menu").querySelector("summary").addEventListener("click",event=>{if($("snapshot").disabled)event.preventDefault();});
function discardDocument() {
  setExportAvailable(false);
  doc?.dispose(); doc=null; activeRecord=null; navigationTarget=null; navigationTask=null;
  document.body.classList.remove("has-structure");
  inspector?.clear();
  $("drift-settings").disabled = true;
}
async function showDocument(store,generation,selection=null) {
  if(generation!==readGeneration){store.dispose();return;}
  doc=store;
  const target=selection?await resolveStructureSelection(store,selection):0;
  const record=await store.get(target);
  if(generation!==readGeneration)return;
  activeRecord=record;
  document.body.classList.add("has-structure");
  $("drift-settings").disabled = false;
  setExportAvailable(true);
  frameIndex=target;streamComplete=store.complete;render(true,true);status('');loading=false;
  // Render the first frame before starting any background index batch.
  afterPaint(()=>store.scan(()=>{if(doc===store){streamComplete=store.complete;updateTimeline();}})
    .catch(error=>{if(doc===store)status(`加载停止：${error.message}（保留已加载的 ${store.count} 帧）`,true);}));
}
function beginTimelineSeek() {
  if(timelineSeek)return;
  const playing=timer!==null;stop();
  timelineSeek={playing,document:doc,generation:readGeneration};doc?.setPlayback?.(frameIndex,true);displayPlayback(playing);
}
async function finishTimelineSeek() {
  const seek=timelineSeek;if(!seek)return;
  await go($("timeline").value);
  if(timelineSeek!==seek||seek.document!==doc||seek.generation!==readGeneration)return;
  timelineSeek=null;updateTimeline();
  if(seek.playing&&viewVisible)startPlayback(false);else {doc?.setPlayback?.(frameIndex,false);displayPlayback(false);}
}
$("timeline").addEventListener("pointerdown",beginTimelineSeek);
$("timeline").addEventListener("input",e=>{beginTimelineSeek();go(e.target.value);});
$("timeline").addEventListener("change",finishTimelineSeek);
window.addEventListener("pointerup",finishTimelineSeek);
window.addEventListener("pointercancel",finishTimelineSeek);
$("timeline").addEventListener("blur",finishTimelineSeek);
$("frame-number").addEventListener("change", e => { stop(); go(Number(e.target.value) - 1); });
$("frame-number").addEventListener("keydown", e => {if(e.key==='Enter'){e.preventDefault();stop();go(Number(e.target.value)-1);e.target.blur();}});
$("previous").onclick = () => { stop(); go(frameIndex - 1); };
$("next").onclick = () => { stop(); go(frameIndex + 1); };
function startPlayback(restart=true) {
  if (timer!==null||!viewVisible||!doc || doc.count < 2) return;
  if(restart&&frameIndex===doc.count-1 && streamComplete)go(0);
  displayPlayback(true);
  doc.setPlayback?.(frameIndex,true);doc.prefetch?.(frameIndex,playback.step);
  const run=++playbackGeneration;
  let previous=performance.now(), accumulator=0;
  const tick=async now=>{
    if(timer===null||run!==playbackGeneration)return;
    accumulator+=Math.min(now-previous,250)*playback.fps*playback.speed/1000;previous=now;
    const steps=Math.floor(accumulator);
    if(steps>0){
      accumulator-=steps;
      let target=frameIndex+steps*playback.step;
      if(target>=doc.count){
        if(!streamComplete){target=doc.count-1;accumulator=0;}
        else if(playback.loop)target%=doc.count;
        else {go(doc.count-1);stop();return;}
      }
      if(target!==frameIndex){await go(target);previous=performance.now();}
    }
    if(timer!==null&&run===playbackGeneration){doc.setPlayback?.(frameIndex,true);doc.prefetch?.(frameIndex,playback.step);timer=requestAnimationFrame(tick);}
  };
  timer=requestAnimationFrame(tick);
}
$("play").onclick=()=>{if(timer!==null||timelineSeek?.playing)return stop();startPlayback();};
function updatePlaybackSettings(){
  const fps=Number($("animation-fps").value),step=Number($("playback-step").value),speed=Number($("playback-speed").value);
  if(!Number.isInteger(fps)||fps<1||fps>120||!Number.isInteger(step)||step<1||step>10000||!Number.isFinite(speed)||speed<=0)return;
  playback={fps,speed,step,loop:$("playback-loop").checked};
}
for(const id of ['animation-fps','playback-speed','playback-step','playback-loop'])$(id).addEventListener('input',updatePlaybackSettings);
$("reset").onclick = () => views?.fit();
$("view-reset").onclick = () => views?.fit();
$("navigation-mode").onchange = event => views?.setMode(event.target.value);
$("orientation").onchange = event => views?.setOrientation(event.target.value);
$("layout").onchange = event => views?.setLayout(event.target.value);
$("expand").onclick = () => views?.toggleExpand();
for (const mode of ["rotate", "pan", "zoom", "select", "box"]) $(mode).onclick = () => views?.setMode(mode);
$("first").onclick = () => { stop(); go(0); };
$("last").onclick = () => { stop(); if (doc) go(doc.count - 1); };
// Display changes repaint the current scene without altering playback intent.
// The existing playback loop reads these same settings for subsequent frames.
for (const id of ["style", "palette", "radius-scale", "cell", "labels", "hbonds", "wrap"]) $(id).onchange = () => { status(""); scheduleRender(); };
function openPanel(panel) { if(!panel.open)panel.show(); }
for(const id of ['display-dialog','drift-dialog'])StructureControls.draggable($(id));
new ResizeObserver(updateTimeline).observe($("ticks"));
inspector=new StructureInspector.Panel(()=>views?.resizeToLayout(),index=>{selection=[index];selectionChanged();});
$("drift-settings").onclick=()=>{
  $("drift-reference").max=doc?.count||1;
  syncDriftScope();
  openPanel($("drift-dialog"));
};
function syncDriftScope(){
  const specified=$("drift-source").value==='indices';
  $("drift-atoms").parentElement.hidden=!specified;
  $("drift-use-selection").hidden=!specified;
}
$("drift-source").onchange=syncDriftScope;
$("drift-close").onclick=()=>$("drift-dialog").close();
$("drift-use-selection").onclick=()=>{
  $("drift-atoms").value=selection.map(i=>i+1).join(', ');
  $("drift-source").value='indices';
};
$("drift-apply").onclick=async()=>{
  if(!doc)return;
  if(!$("drift-enabled").checked){clearFollow();$("drift-error").textContent='';render();return;}
  $("drift-apply").disabled=true;
  try {
    const reference=StructureControls.referenceIndex($("drift-reference").value,doc.count);
    const currentDoc=doc,generation=readGeneration,currentFrame=frameData;
    const currentIndices=$("drift-source").value==='all'?currentFrame.atoms.map((_,i)=>i):StructureControls.atomIndices($("drift-atoms").value,currentFrame.atoms.length);
    const referenceFrame=(await currentDoc.get(reference)).frame;
    if(doc!==currentDoc||generation!==readGeneration)return;
    let candidate=null;
    if($("drift-enabled").checked){
      const indices=StructureSelection.remap(currentFrame,referenceFrame,currentIndices);
      if(indices.length!==currentIndices.length)throw Error('所指定原子无法与参考帧对应');
      candidate={reference,referenceFrame,indices};
      StructureGeometry.removeDrift(frameData,referenceFrame,StructureSelection.remap(referenceFrame,frameData,indices),indices);
    }
    drift=candidate;$("drift-error").textContent='';render();
    $("drift-state").textContent=drift?`已应用：${drift.indices.length} 个原子 · 参考帧 ${reference+1}`:'';
    $("drift-state").hidden=!drift;
  }catch(error){$("drift-error").textContent=error.message;}finally{$("drift-apply").disabled=false;}
};
$("source-toggle").onclick = () => { stop(); $("source-panel").hidden = !$("source-panel").hidden; if(doc)$("source").value=activeRecord.text; };
async function openStructure(files, format = $("import-format").value, selection=null, options={}) {
  const file = files[0];
  const generation = ++readGeneration;
  stop(); clearFollow(); resetDisplayState(); discardDocument();
  ++snapshotTicket; snapshotData = null;
  for (const id of ["snapshot-dialog", "drift-dialog", "display-dialog"]) $(id).close();
  frameIndex = 0; frameData = null; loading = true; streamComplete = false;
  if (views) {
    for (const pane of views.panes) {
      pane.ready = false;
      pane.viewer.removeAllModels(); pane.viewer.removeAllShapes(); pane.viewer.removeAllLabels();
      pane.viewer.render();
    }
  }
  $("atom-title").textContent = $("measurement").textContent = $("atom-list").textContent = "";
  $("frame-total").textContent = "—"; tickLayout=''; $("timeline").disabled = true;
  $("source-panel").hidden = true; $("source-toggle").hidden = /\.traj$/i.test(file.name);
  $("filename").removeAttribute("data-empty-title");
  $("filename").textContent = file.name; status("");
  $("import-note").textContent = "";
  try {
    const imported = await StructureImport.open(files, {format,...options});
    if (generation !== readGeneration) { imported.store.dispose(); return; }
    $("filename").textContent = imported.name;
    $("source-toggle").hidden = /\.traj$/i.test(imported.name);
    await showDocument(imported.store, generation, selection);
    if (generation !== readGeneration) return;
    $("import-note").textContent = imported.note;
    return {name: imported.name,generation,frameIndex,frameCount:doc.count,ionicStep:doc.source.ionicSteps?.[frameIndex] ?? null,simulationStep:doc.source.simulationSteps?.[frameIndex] ?? null};
  } catch (error) {
    if (generation !== readGeneration) return;
    discardDocument();
    status(error.message, true);
    $("import-note").textContent = "";
    return {name: file.name, error: error.message};
  } finally { if (generation === readGeneration) loading = false; }
}
async function resolveStructureSelection(store,selection) {
  let index;
  if(Number.isSafeInteger(selection?.simulationStep)) {
    index=await store.findSimulationStep(selection.simulationStep,{nearest:true});
  } else if(Number.isSafeInteger(selection?.ionicStep)) {
    if(selection.stride && selection.stride!==1)throw Error('该轨迹不是逐离子步保存，无法确认所选步对应的结构');
    index=await store.findIonicStep(selection.ionicStep);
  } else if(Number.isSafeInteger(selection?.frameIndex)) {
    while(store.count<=selection.frameIndex && !store.complete)await store.advance();
    index=selection.frameIndex;
  } else throw Error('请选择有效的轨迹帧');
  if(index<0 || index>=store.count)throw Error('所选帧尚未写入轨迹');
  return index;
}
async function seekStructure(selection, generation=readGeneration) {
  const currentDoc=doc;
  if(!currentDoc || generation!==readGeneration)throw Error('结构已切换，请重新打开该步骤的轨迹');
  stop();
  const index=await resolveStructureSelection(currentDoc,selection);
  if(doc!==currentDoc || generation!==readGeneration)return;
  await go(index);
  if(doc!==currentDoc || generation!==readGeneration)return;
  if(frameIndex!==index)throw Error('轨迹帧读取未完成，请重试');
  return {frameIndex,frameCount:doc.count,ionicStep:currentDoc.source.ionicSteps?.[frameIndex] ?? null,simulationStep:currentDoc.source.simulationSteps?.[frameIndex] ?? null};
}
$("open-empty").onclick = $("open-file").onclick = () => $("file-picker").click();
$("file-picker").onchange = () => {
  const files = Array.from($("file-picker").files); $("file-picker").value = "";
  if (files.length) openStructure(files);
};
document.addEventListener("dragover", event => event.preventDefault());
document.addEventListener("drop", event => {
  event.preventDefault(); const files = Array.from(event.dataTransfer.files);
  if (files.length) openStructure(files);
});
window.addEventListener("pagehide", () => { stop(); discardDocument(); });
function snapshotOptions(){return {dpi:Number($("snapshot-dpi").value),format:$("snapshot-format").value,transparent:$("snapshot-transparent").checked,crop:$("snapshot-crop").checked};}
async function refreshSnapshot(){
  const ticket=++snapshotTicket;snapshotData=null;$("snapshot-save").disabled=true;$("snapshot-result").textContent='';
  try {
    const result=await StructureSnapshot.capture(views,snapshotOptions());
    if(ticket!==snapshotTicket)return;snapshotData=result;$("snapshot-preview").src=result.preview;$("snapshot-preview").hidden=false;$("snapshot-save").disabled=false;
  }catch(error){if(ticket===snapshotTicket)$("snapshot-result").textContent=error.message;}
}
$("snapshot").onclick=async()=>{
  if(!views||!doc)return;stop();$("snapshot-name").value=$("filename").textContent.replace(/\.[^.]+$/,'').replace(/[^\p{L}\p{N}_ .()\-]/gu,'_')+'-frame-'+(frameIndex+1);
  snapshotData=null;$("snapshot-save").disabled=true;$("snapshot-preview").hidden=true;
  $("snapshot-dialog").showModal();
  $("snapshot-result").textContent='';afterPaint(refreshSnapshot);
};
for(const id of ["snapshot-format","snapshot-dpi","snapshot-transparent","snapshot-crop"])$(id).onchange=()=>{
  snapshotData=null;$("snapshot-save").disabled=true;
  $("snapshot-transparent").disabled=$("snapshot-format").value!=='png';
  $("snapshot-transparent").parentElement.hidden=$("snapshot-transparent").disabled;afterPaint(refreshSnapshot);
};
$("snapshot-close").onclick=()=>{$("snapshot-dialog").close();++snapshotTicket;};
$("snapshot-save").onclick = () => {
  if (!snapshotData) return;
  const format = $("snapshot-format").value;
  const name = $("snapshot-name").value.replace(/[^\p{L}\p{N}_ .()\-]/gu, "_") || "structure";
  const url = URL.createObjectURL(new Blob([snapshotData.bytes], {type: {png:"image/png", jpeg:"image/jpeg", pdf:"application/pdf"}[format]}));
  const anchor = document.createElement("a");
  anchor.href = url; anchor.download = `${name}.${format}`; anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
  $("snapshot-result").textContent = "已开始下载";
};

const headerMenus = [...document.querySelectorAll(".header-menu,.bottom-menu")];
for (const menu of headerMenus) {
  menu.addEventListener("toggle", () => {
    if(menu.open)for(const other of headerMenus)if(other!==menu)other.open=false;
  });
  menu.addEventListener("click", event => {
    // Leave selects and checkboxes open for repeated adjustments. An action
    // can open its own dialog; do not steal focus back from that dialog.
    if(event.target.closest("[data-close-menu]"))menu.open=false;
  });
}
document.addEventListener("pointerdown", event => {
  if(event.target.closest("#chem-choice-menu"))return;
  for(const menu of headerMenus)if(menu.open&&!menu.contains(event.target))menu.open=false;
});
window.addEventListener("keydown", event => {
  const preset=axisShortcut(event);
  if(preset && views?.panes[views.active].ready && !document.querySelector("dialog:modal")){
    event.preventDefault();
    views.setOrientation(preset);
  }
  if (event.key === "Escape" && !document.querySelector("#chem-choice-menu")) {
    const menu=headerMenus.find(menu=>menu.open);
    if(menu){menu.open=false;menu.querySelector("summary").focus();event.preventDefault();}
  }
});
const themeControls = {mode: $("theme-mode"), fontSize: $("theme-size")};
function syncThemeControls() {
  const settings = chemistryTheme.read();
  themeControls.mode.value = settings.mode;
  themeControls.fontSize.value = String(settings.fontSize);
}
for (const control of Object.values(themeControls)) control.onchange = () => {
  chemistryTheme.save({mode:themeControls.mode.value,fontSize:Number(themeControls.fontSize.value)});
};
syncThemeControls();
