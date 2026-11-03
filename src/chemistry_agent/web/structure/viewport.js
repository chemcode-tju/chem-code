"use strict";
const viewQuaternions = {xy: [0, 0, 0, 1], xz: [-Math.SQRT1_2, 0, 0, Math.SQRT1_2], yz: [-0.5, 0.5, 0.5, 0.5], perspective: [-Math.SQRT1_2, 0, 0, Math.SQRT1_2]};
function projectToView([a,b,c], [x,y,z,w]) {
  // Match the renderer's model matrix (3Dmol Vector3.applyQuaternion uses its inverse).
  return [(1-2*(y*y+z*z))*a + 2*(x*y-z*w)*b + 2*(x*z+y*w)*c,
    2*(x*y+z*w)*a + (1-2*(x*x+z*z))*b + 2*(y*z-x*w)*c,
    2*(x*z-y*w)*a + 2*(y*z+x*w)*b + (1-2*(x*x+y*y))*c];
}
function labelScreenScale(diameter, fontSize, textWidth) {
  // No minimum: distant tiny atoms must not be buried under oversized IDs.
  if(!Number.isFinite(diameter)||diameter<=0)return 0;
  return Math.min(1,0.85*diameter/Math.max(fontSize,textWidth));
}
class StructureViews {
  constructor(container, onAtom, onBox) {
    this.container = container;
    this.onAtom = onAtom; this.onBox = onBox;
    this.panes = [];
    this.active = 1;
    this.layout = "single";
    this.previousLayout = "quad";
    this.mode = "rotate";
    ["xy", "xz", "yz", "perspective"].forEach((preset, index) => this.createPane(preset, index));
    this.setLayout("single");
  }
  createPane(preset, index) {
    const element = document.createElement("section");
    element.className = "viewport";
    element.innerHTML = '<button class="view-title" type="button"></button><div class="view-canvas"></div><div class="pointer-layer"><div class="selection-rectangle" hidden></div></div><div class="selection-rectangle shortcut-rectangle" hidden></div><svg class="axis-gizmo" viewBox="0 0 78 78" aria-label="坐标轴"></svg>';
    this.container.append(element);
    const canvas = element.querySelector(".view-canvas");
    const viewer = $3Dmol.createViewer(canvas, {canvas: document.createElement("canvas"), row:0, col:0, rows:1, cols:1, backgroundColor: "#ffffff", antialias: true, orthographic: true, disableFog: true, manualResize: true});
    const pane = {element, canvas, viewer, preset, ready: false};
    this.panes.push(pane);
    element.querySelector(".view-title").textContent = this.title(preset);
    element.querySelector(".view-title").onclick = () => this.activate(index);
    element.querySelector(".view-title").ondblclick = () => { this.activate(index); this.toggleExpand(); };
    element.addEventListener("pointerdown", () => { this.commitResize(pane); this.activate(index); }, true);
    element.addEventListener("mousedown", () => this.activate(index), true);
    viewer.setViewChangeCallback(() => { this.drawAxes(pane); this.drawSelection(pane); });
    // Own resize scheduling; the pinned renderer's automatic listeners are
    // disabled with manualResize. resize() already paints the existing scene.
    new ResizeObserver(() => this.scheduleResize(pane)).observe(canvas);
    this.bindShiftSelection(pane);
    let rightPointer=null;
    element.addEventListener('contextmenu',event=>event.preventDefault());
    element.addEventListener('mousedown',event=>{if(event.button===2){event.preventDefault();event.stopImmediatePropagation();}},true);
    element.addEventListener('pointerdown',event=>{
      if(event.button!==2||event.target.closest('button'))return;
      rightPointer={id:event.pointerId,x:event.clientX,y:event.clientY};
      element.setPointerCapture(event.pointerId);event.preventDefault();event.stopImmediatePropagation();
    },true);
    element.addEventListener('pointermove',event=>{
      if(!rightPointer||event.pointerId!==rightPointer.id)return;
      viewer.translateScene(event.clientX-rightPointer.x,event.clientY-rightPointer.y);
      rightPointer.x=event.clientX;rightPointer.y=event.clientY;event.stopImmediatePropagation();
    },true);
    for(const name of ['pointerup','pointercancel'])element.addEventListener(name,event=>{
      if(rightPointer?.id===event.pointerId){rightPointer=null;event.stopImmediatePropagation();}
    },true);
    const layer = element.querySelector(".pointer-layer");
    let pointer = null;
    layer.onpointerdown = event => {
      if (event.button !== 0) return;
      pointer = {id: event.pointerId, x: event.clientX, y: event.clientY, startX:event.clientX, startY:event.clientY,mode:this.mode,dragged:false};
      if(this.mode==='box') {const box=layer.querySelector('.selection-rectangle');box.hidden=false;box.style.cssText='left:0;top:0;width:0;height:0';}
      layer.setPointerCapture(event.pointerId); event.preventDefault();
    };
    layer.onpointermove = event => {
      if (!pointer || pointer.id !== event.pointerId) return;
      const dx = event.clientX - pointer.x, dy = event.clientY - pointer.y;
      if(Math.hypot(event.clientX-pointer.startX,event.clientY-pointer.startY)>4)pointer.dragged=true;
      if(pointer.mode==='box') {
        const rect=layer.getBoundingClientRect(),box=layer.querySelector('.selection-rectangle');
        box.style.cssText=`left:${Math.min(pointer.startX,event.clientX)-rect.left}px;top:${Math.min(pointer.startY,event.clientY)-rect.top}px;width:${Math.abs(event.clientX-pointer.startX)}px;height:${Math.abs(event.clientY-pointer.startY)}px`;
      } else if (this.mode === "pan") viewer.translateScene(dx, dy);
      else viewer.zoom(Math.exp(-dy * 0.012));
      pointer.x = event.clientX; pointer.y = event.clientY;
    };
    layer.onpointerup = event => {
      if(pointer?.id===event.pointerId&&pointer.mode==='box'&&pointer.dragged) {
        const atoms=this.latest?.[0].atoms||[];
        const points=this.visibleIndices.map(index=>({...viewer.modelToScreen(atoms[index]),index}));
        this.onBox(StructureSelection.inRectangle(points,{x:pointer.startX+scrollX,y:pointer.startY+scrollY},{x:event.clientX+scrollX,y:event.clientY+scrollY}),event.shiftKey);
      }
      layer.querySelector('.selection-rectangle').hidden=true;pointer=null;
    };
    layer.onpointercancel=layer.onlostpointercapture=()=>{layer.querySelector('.selection-rectangle').hidden=true;pointer=null;};
    layer.addEventListener("wheel", event => { event.preventDefault(); viewer.zoom(Math.exp(-event.deltaY * 0.001)); }, {passive: false});
  }
  scheduleResize(pane) {
    const width=pane.canvas.clientWidth,height=pane.canvas.clientHeight;
    const size=`${width}:${height}:${globalThis.devicePixelRatio||1}`;
    if(pane.element.hidden||width<=0||height<=0)return;
    if(!pane.resizeTimer&&pane.lastSize===size)return;
    // Keep an opaque copy over the WebGL surface while the host changes its
    // compositor backing layer. Only resize the GPU buffer after dragging stops.
    if(!pane.resizeCover&&pane.ready){
      const source=pane.canvas.querySelector('canvas');
      if(source?.width&&source?.height){
        const cover=document.createElement('canvas');cover.className='resize-cover';
        cover.width=source.width;cover.height=source.height;
        try{cover.getContext('2d').drawImage(source,0,0);pane.element.append(cover);pane.resizeCover=cover;}catch{}
      }
    }
    clearTimeout(pane.resizeTimer);
    cancelAnimationFrame(pane.resizeReveal);
    pane.resizeTimer=setTimeout(()=>this.commitResize(pane),140);
  }
  commitResize(pane) {
    if(!pane.resizeTimer)return;
    clearTimeout(pane.resizeTimer);pane.resizeTimer=null;
    cancelAnimationFrame(pane.resizeReveal);
    const width=pane.canvas.clientWidth,height=pane.canvas.clientHeight;
    if(!pane.element.hidden&&width>0&&height>0){
      const size=`${width}:${height}:${globalThis.devicePixelRatio||1}`;
      if(pane.lastSize!==size){pane.viewer.resize();pane.lastSize=size;}
    }
    // Let the new WebGL frame reach the compositor before uncovering it.
    pane.resizeReveal=requestAnimationFrame(()=>{
      pane.resizeReveal=requestAnimationFrame(()=>{
        if(!pane.resizeTimer){pane.resizeCover?.remove();pane.resizeCover=null;}
      });
    });
  }
  bindShiftSelection(pane) {
    const {element,canvas,viewer}=pane;
    const rectangle=element.querySelector('.shortcut-rectangle');
    let gesture=null;
    const stop=event=>{event.preventDefault();event.stopImmediatePropagation();};
    const finish=()=>{rectangle.hidden=true;gesture=null;};
    // Capture before either 3Dmol or the active navigation overlay handles input.
    element.addEventListener('pointerdown',event=>{
      if(event.button!==0||!event.shiftKey||event.target.closest('button'))return;
      gesture={id:event.pointerId,x:event.clientX,y:event.clientY,dragged:false};
      element.setPointerCapture(event.pointerId);stop(event);
    },true);
    element.addEventListener('pointermove',event=>{
      if(!gesture||gesture.id!==event.pointerId)return;
      if(Math.hypot(event.clientX-gesture.x,event.clientY-gesture.y)>4)gesture.dragged=true;
      if(gesture.dragged){
        const rect=canvas.getBoundingClientRect();rectangle.hidden=false;
        rectangle.style.cssText=`left:${Math.min(gesture.x,event.clientX)-rect.left}px;top:${Math.min(gesture.y,event.clientY)-rect.top}px;width:${Math.abs(event.clientX-gesture.x)}px;height:${Math.abs(event.clientY-gesture.y)}px`;
      }
      stop(event);
    },true);
    element.addEventListener('pointerup',event=>{
      if(!gesture||gesture.id!==event.pointerId)return;
      stop(event);
      const current=gesture;finish();
      if(current.dragged){
        const atoms=this.latest?.[0].atoms||[];
        const points=this.visibleIndices.map(index=>({...viewer.modelToScreen(atoms[index]),index}));
        this.onBox(StructureSelection.inRectangle(points,{x:current.x+scrollX,y:current.y+scrollY},{x:event.clientX+scrollX,y:event.clientY+scrollY}),true);
      }else{
        const rect=canvas.getBoundingClientRect();
        if(!rect.width||!rect.height)return;
        const hits=viewer.targetedObjects(2*(event.clientX-rect.left)/rect.width-1,1-2*(event.clientY-rect.top)/rect.height,{});
        if(hits.length){const index=this.visibleIndices[hits[0].clickable.index];if(index!==undefined)this.onAtom(index,true);}
      }
    },true);
    for(const name of ['pointercancel','lostpointercapture'])element.addEventListener(name,event=>{
      if(gesture?.id===event.pointerId)finish();
    },true);
    for(const name of ['mousedown','mouseup','click'])element.addEventListener(name,event=>{
      if((gesture||event.shiftKey)&&!event.target.closest('button'))stop(event);
    },true);
  }
  title(preset) { return {xy: "Top · XY", xz: "Front · XZ", yz: "Left · YZ", perspective: "Perspective"}[preset]; }
  activate(index) {
    this.active = index;
    this.panes.forEach((pane, i) => pane.element.classList.toggle("active", i === index));
    document.getElementById("orientation").value = this.panes[index].preset;
  }
  setLayout(layout) {
    this.layout = layout;
    this.container.dataset.layout = layout;
    const visible = layout === "single" ? [this.active] : layout === "dual" ? [0, 1] : [0, 1, 2, 3];
    if (!visible.includes(this.active)) this.activate(1);
    this.panes.forEach((pane, index) => { pane.element.hidden = !visible.includes(index); });
    this.activate(this.active);
    if (this.scene) this.renderScene(this.scene,this.latest[2],false);
    document.getElementById("layout").value = layout;
    const single = layout === "single";
    document.getElementById("expand").querySelector("use").setAttribute("href", "/assets/ui-icons.svg#i-" + (single ? "multi-view" : "expand"));
    requestAnimationFrame(() => this.panes.forEach(pane => {
      if (!pane.element.hidden) {
        pane.viewer.resize();
        if (pane.ready) this.fit(pane);
        pane.viewer.render();
      }
    }));
  }
  toggleExpand() {
    if (this.layout === "single") this.setLayout(this.previousLayout);
    else { this.previousLayout = this.layout; this.setLayout("single"); }
  }
  setMode(mode) {
    this.mode = mode;
    this.container.dataset.mode = mode;
    document.querySelectorAll("[data-mode-button]").forEach(button => button.setAttribute("aria-pressed", String(button.dataset.modeButton === mode)));
  }
  orient(pane, preset) {
    pane.preset = preset;
    const q = viewQuaternions[preset];
    const view = pane.viewer.getView();
    view.splice(4, 4, ...q);
    pane.viewer.setView(view);
    pane.viewer.setProjection(preset === "perspective" ? "perspective" : "orthographic");
    if (preset === "perspective") { pane.viewer.rotate(18, "x"); pane.viewer.rotate(-25, "y"); }
    pane.element.querySelector(".view-title").textContent = this.title(preset);
    this.fit(pane);
  }
  setOrientation(preset) { this.orient(this.panes[this.active], preset); this.activate(this.active); }
  fit(pane = this.panes[this.active]) {
    if (!pane.ready || pane.element.hidden || !this.bounds) return;
    const {min, max} = this.bounds;
    const center = this.follow ? [this.follow.x,this.follow.y,this.follow.z] : min.map((n, i) => (n + max[i]) / 2);
    const view = pane.viewer.getView();
    const projected = Array.from({length:8}, (_,mask) => projectToView(min.map((n,i) => ((mask>>i)&1 ? max[i] : n) - center[i]), view.slice(4,8)));
    const halfX = Math.max(...projected.map(v => Math.abs(v[0]))), halfY = Math.max(...projected.map(v => Math.abs(v[1]))), halfZ = Math.max(...projected.map(v => Math.abs(v[2])));
    const aspect = pane.canvas.clientWidth / Math.max(1, pane.canvas.clientHeight);
    const distance = pane.preset === "perspective" ? Math.max(halfY, halfX/aspect)/Math.tan(Math.PI/18) + halfZ : Math.max(halfX, halfY*aspect)/Math.tan(Math.PI/9);
    view.splice(0, 4, ...center.map(n => -n), 150 - Math.max(5, distance)*1.15);
    pane.viewer.setSlab(-Math.hypot(...max.map((n,i) => n-min[i]))*2, Math.hypot(...max.map((n,i) => n-min[i]))*2);
    pane.viewer.setView(view); pane.viewer.render();
  }
  drawAxes(pane) {
    const q = pane.viewer.getView().slice(4, 8), [x, y, z, w] = q;
    const axes = [[1-2*(y*y+z*z), 2*(x*y+z*w)], [2*(x*y-z*w), 1-2*(x*x+z*z)], [2*(x*z+y*w), 2*(y*z-x*w)]];
    pane.element.querySelector(".axis-gizmo").innerHTML = axes.map(([dx, dy], index) => {
      const endX = 31 + 26*dx, endY = 46 - 26*dy, color = ["#db3434", "#259942", "#4964e9"][index];
      return `<line x1="31" y1="46" x2="${endX}" y2="${endY}" stroke="${color}" stroke-width="1.5"/><text x="${endX+3}" y="${endY-3}" fill="${color}">${["x", "y", "z"][index]}</text>`;
    }).join("");
  }
  drawSelection(pane) {
    if(pane.drawingSelection)return;
    const viewer=pane.viewer,view=viewer.getView(),key=JSON.stringify([view,pane.preset,pane.canvas.clientWidth,pane.canvas.clientHeight]);
    // Context recovery recreates label materials and resets sprite scales even
    // when the camera is unchanged. Reapply position/scale in that case.
    const labelMaterial=pane.atomLabels?.[0]?.label.sprite.material;
    if(pane.markerView===key&&pane.labelMaterial===labelMaterial)return;
    pane.labelMaterial=labelMaterial;
    if(!pane.markers?.length&&!pane.atomLabels?.length){pane.markerView=key;return;}
    pane.drawingSelection=true;
    try {
      for(const shape of [...(pane.markerShapes||[])].reverse())viewer.removeShape(shape);
      pane.markerShapes=[];
      const inverse=new $3Dmol.Matrix4().getInverse(viewer.modelGroup.matrixWorld);
      const camera=viewer.camera.position.clone().applyMatrix4(inverse);
      const q=view.slice(4,8),[x,y,z,w]=q;
      const forward=[inverse.elements[8],inverse.elements[9],inverse.elements[10]];
      for(const {label,atom,radius,sampling,fontSize,textWidth} of pane.atomLabels||[]) {
        const delta=pane.preset==='perspective'?[camera.x-atom.x,camera.y-atom.y,camera.z-atom.z]:forward;
        const norm=Math.hypot(...delta),offset=radius*1.01;
        label.sprite.position.set(atom.x+delta[0]/norm*offset,atom.y+delta[1]/norm*offset,atom.z+delta[2]/norm*offset);
        const origin=viewer.modelToScreen(atom);
        const unitsPerPixel=viewer.screenToModelDistance({x:origin.x+1,y:origin.y},atom);
        const factor=labelScreenScale(2*radius/unitsPerPixel,fontSize,textWidth)/sampling;
        label.sprite.scale.set(factor,factor,1);
      }
      let combined={vertexArr:[],normalArr:[],faceArr:[],color:0x004500,opacity:1};
      const flush=()=>{if(combined.vertexArr.length)pane.markerShapes.push(viewer.addCustom(combined));combined={vertexArr:[],normalArr:[],faceArr:[],color:0x004500,opacity:1};};
      for(const {atom,radius} of pane.markers||[]) {
        const delta=[camera.x-atom.x,camera.y-atom.y,camera.z-atom.z];
        const distance=pane.preset==='perspective'?Math.hypot(...delta):Infinity;
        const normal=Number.isFinite(distance)?delta:forward;
        const origin=viewer.modelToScreen(atom);
        const unitsPerPixel=viewer.screenToModelDistance({x:origin.x+2.5,y:origin.y},atom);
        if(combined.vertexArr.length>32000)flush();
        const mesh=StructureSelection.outlineRing(atom,radius,normal,Math.min(radius*.12,unitsPerPixel),distance),offset=combined.vertexArr.length;
        combined.vertexArr.push(...mesh.vertexArr);combined.normalArr.push(...mesh.normalArr);
        combined.faceArr.push(...mesh.faceArr.map(i=>i+offset));
      }
      flush();
      pane.markerView=key;viewer.render();
    } finally {pane.drawingSelection=false;}
  }
  updateSelection(selected) {
    if(!this.latest)return;
    this.latest[2]=selected;
    const [frame,options]=this.latest;
    for(const pane of this.panes) {
      if(pane.element.hidden)continue;
      for(const shape of [...(pane.markerShapes||[]),...(pane.measureShapes||[])].sort((a,b)=>b.shapePosition-a.shapePosition))pane.viewer.removeShape(shape);
      pane.markerShapes=[];pane.measureShapes=[];pane.markers=[];pane.markerView=null;
      this.prepareSelection(pane,frame,options,selected);
      pane.viewer.render();
    }
  }
  prepareSelection(pane,frame,options,selected) {
    const visibleSet=new Set(this.visibleIndices);
    pane.markers=selected.filter(i=>visibleSet.has(i)).map(i=>{
      const atom=frame.atoms[i],rule=this.rules[i];
      return {atom,radius:rule.style==='stick'?.19:StructureAppearance.radius(atom.elem)*options.scale*(rule.style==='ballstick'?.45:1)};
    });
    pane.measureShapes=[];
    if(selected.length>=2&&selected.length<=4)for(let j=1;j<selected.length;j++) {
      if(visibleSet.has(selected[j-1])&&visibleSet.has(selected[j]))pane.measureShapes.push(pane.viewer.addLine({start:frame.atoms[selected[j-1]],end:frame.atoms[selected[j]],color:'#075dce',linewidth:2}));
    }
  }
  setLighting(viewer, profile = StructureScene.lighting) {
    // Match the studio shaders bundled by server.py, on screen and in exports.
    const light = viewer.scene?.__lights?.[0];
    if (light) { light.position.set(...profile.direction).normalize(); light.intensity = profile.intensity; }
    viewer.setViewStyle({style: ""});
  }
  captureScene() {
    const pane=this.panes[this.active];
    return Object.freeze({scene:this.scene,preset:pane.preset,view:pane.viewer.getView().slice(),
      width:pane.canvas.clientWidth,height:pane.canvas.clientHeight,
      pixelRatio:pane.canvas.querySelector('canvas').width/pane.canvas.clientWidth});
  }
  renderScene(scene,selected=[],reset=false) {
    this.scene=scene;
    this.render(scene.frame,scene.options,selected,reset);
  }
  exportPane(width, height, capture=this.captureScene()) {
    if (!this.exportView) {
      const element=document.createElement('div');
      element.setAttribute('aria-hidden','true');
      element.style.cssText='position:fixed;left:-20000px;top:0;pointer-events:none';
      element.innerHTML='<div class="view-title"></div><svg class="axis-gizmo"></svg><div class="export-canvas"></div>';
      document.body.append(element);
      const canvas=element.querySelector('.export-canvas');
      canvas.style.width=width+'px';canvas.style.height=height+'px';
      const viewer=$3Dmol.createViewer(canvas,{canvas:document.createElement('canvas'),row:0,col:0,rows:1,cols:1,backgroundColor:'#ffffff',backgroundAlpha:0,antialias:true,orthographic:true,disableFog:true,nomouse:true,manualResize:true});
      const pane={element,canvas,viewer,preset:'xz',ready:false};
      const exporter=Object.create(StructureViews.prototype);
      Object.assign(exporter,{panes:[pane],onAtom:()=>{},active:0});
      this.exportView={pane,exporter};
    }
    const {pane,exporter}=this.exportView;
    pane.canvas.style.width=width+'px';pane.canvas.style.height=height+'px';pane.element.style.width=width+'px';pane.element.style.height=height+'px';
    pane.viewer.resize();pane.preset=capture.preset;
    exporter.renderScene({...capture.scene,options:{...capture.scene.options,labelScale:width/capture.width}},[],false);
    pane.viewer.setProjection(pane.preset==='perspective'?'perspective':'orthographic');
    pane.viewer.setView(capture.view.slice());
    pane.viewer.setBackgroundColor('#ffffff',0);pane.viewer.render();
    exporter.drawSelection(pane);
    return pane;
  }
  render(frame, options, selected, reset) {
    this.latest = [frame, options, selected];
    this.follow = options.follow || null;
    const rules=frame.atoms.map((atom,index)=>options.display?.resolve(atom,index,options)||{...options,visible:true});
    this.rules=rules;
    this.visibleIndices=rules.flatMap((rule,index)=>rule.visible?[index]:[]);
    const visibleSet=new Set(this.visibleIndices);
    const hydrogenBonds = options.hbonds ? StructureGeometry.hydrogenBonds(frame).filter(b=>visibleSet.has(b.donor)&&visibleSet.has(b.hydrogen)&&visibleSet.has(b.acceptor)) : [];

    const vertices = options.cell && frame.cell ? Array.from({length:8}, (_,mask) => Object.fromEntries(["x","y","z"].map((axis,j) => [axis, frame.cell.reduce((sum,v,k) => sum + ((mask>>k)&1)*v[j], 0)]))) : [];
    this.bounds = {min:[Infinity,Infinity,Infinity], max:[-Infinity,-Infinity,-Infinity]};
    for (const atom of [...this.visibleIndices.map(i=>frame.atoms[i]), ...vertices]) {
      const radius = atom.elem ? StructureAppearance.radius(atom.elem)*options.scale : 0;
      ["x","y","z"].forEach((axis,i) => { this.bounds.min[i] = Math.min(this.bounds.min[i], atom[axis]-radius); this.bounds.max[i] = Math.max(this.bounds.max[i], atom[axis]+radius); });
    }
    if(!Number.isFinite(this.bounds.min[0]))this.bounds={min:[-1,-1,-1],max:[1,1,1]};
    for (const pane of this.panes) {
      if (pane.element.hidden) { pane.stale = true; continue; }
      const viewer = pane.viewer, previous = viewer.getView();
      const initialize = !pane.ready;
      this.setLighting(viewer, options.lighting);
      pane.drawingSelection=true;
      pane.markers=[];pane.markerShapes=[];pane.markerView=null;
      viewer.removeAllModels(); viewer.removeAllShapes(); viewer.removeAllLabels();
      // Build only visible atoms, so hidden atoms cannot leave bonds or click targets.
      const visibleAtoms=this.visibleIndices.map(i=>frame.atoms[i]);
      const xyz = `${visibleAtoms.length}\n\n${visibleAtoms.map(a => `${a.elem} ${a.x} ${a.y} ${a.z}`).join("\n")}\n`;
      if(visibleAtoms.length) {
        const model = viewer.addModel(xyz, "xyz", {assignBonds: rules.some(r=>r.visible&&r.style!=="sphere")});
        model.selectedAtoms({}).forEach((atom,index)=>{atom.index=index;});
        viewer.setStyle({},{});
        const groups=new Map();
        this.visibleIndices.forEach((originalIndex,index)=>{
          const atom=frame.atoms[originalIndex],rule=rules[originalIndex];
          const palette=StructureAppearance.palette(rule.palette,$3Dmol.elementColors);
          const color=rule.palette==='custom'?rule.color:(palette[atom.elem]??0xff1493);
          const radius=StructureAppearance.radius(atom.elem)*options.scale;
          const style=rule.style==='sphere'?{sphere:{radius,color}}:rule.style==='stick'?{stick:{radius:.16,color}}:{sphere:{radius:radius*.45,color},stick:{radius:.10,color}};
          const key=JSON.stringify(style);if(!groups.has(key))groups.set(key,{style,indices:[]});groups.get(key).indices.push(index);
        });
        for(const {style,indices} of groups.values())viewer.setStyle({index:indices},style);
        model.setClickable({},true,atom=>this.onAtom(this.visibleIndices[atom.index]));
      }
      if (options.cell && frame.cell) {
        for (let mask=0; mask<8; mask++) for (let k=0; k<3; k++) if (!(mask & (1<<k))) viewer.addLine({start:vertices[mask], end:vertices[mask | (1<<k)], color:"#333333", linewidth:1});
      }
      for (const bond of hydrogenBonds) {
        const length = Math.hypot(...bond.end.map((x,i)=>x-bond.start[i]));
        const steps = Math.max(2, Math.ceil(length / 0.16));
        for(let i=0;i<steps;i+=2) {
          const p=t=>Object.fromEntries(["x","y","z"].map((axis,j)=>[axis,bond.start[j]+(bond.end[j]-bond.start[j])*t/steps]));
          viewer.addCylinder({start:p(i),end:p(Math.min(i+1,steps)),radius:0.035,color:0x3699ae,fromCap:1,toCap:1});
        }
      }
      pane.atomLabels=[];
      if(options.labels)this.visibleIndices.forEach(i=>{
        const atom=frame.atoms[i],rule=rules[i];
        const radius=rule.style==='stick'?.19:StructureAppearance.radius(atom.elem)*options.scale*(rule.style==='ballstick'?.45:1);
        // Rasterize above device resolution, then shrink the sprite to keep
        // the same screen size. A 13px texture was enlarged on Retina screens.
        const sampling=Math.max(3,Math.ceil(globalThis.devicePixelRatio||1));
        const fontSize=14*(options.labelScale||1);
        const label=viewer.addLabel(String(i+1),{position:atom,font:'Arial',fontSize:Math.round(fontSize*sampling),bold:true,fontColor:'#000000',fontOpacity:1,borderThickness:0,padding:4*sampling,showBackground:false,inFront:false,alignment:'center'},undefined,true);
        label.sprite.scale.set(1/sampling,1/sampling,1);
        pane.atomLabels.push({atom,radius,label,sampling,fontSize,textWidth:label.context.measureText(String(i+1)).width/sampling});
      });
      this.prepareSelection(pane,frame,options,selected);
      pane.ready = true;
      if (reset || initialize) this.orient(pane, pane.preset);
      else {
        if (this.follow) previous.splice(0,3,-this.follow.x,-this.follow.y,-this.follow.z);
        viewer.setView(previous);
      }
      pane.drawingSelection=false;pane.markerView=null;
      if (!pane.element.hidden) { viewer.render(); this.drawSelection(pane); }
    }
  }
}
if (typeof module !== "undefined" && module.exports) module.exports = {projectToView, viewQuaternions, StructureViews, labelScreenScale};
