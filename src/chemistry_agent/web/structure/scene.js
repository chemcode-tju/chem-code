/* Shared scene settings for interactive preview, stills and animation. */
(function(root) {
  'use strict';
  const lighting=Object.freeze({direction:Object.freeze([-.45,.65,1]),intensity:1});
  function cloneDisplay(display) {
    const copy=new StructureSelection.DisplayState();
    copy.hiddenElements=new Set(display?.hiddenElements);
    copy.overrides=new Map([...(display?.overrides||[])].map(([index,rule])=>[index,{...rule}]));
    return copy;
  }
  function settings(frame,options) {
    return Object.freeze({...options,sourceFrame:frame,display:cloneDisplay(options.display),
      drift:options.drift?Object.freeze({...options.drift,indices:[...options.drift.indices]}):null,lighting});
  }
  function transform(frame,settings) {
    if(settings.drift) {
      const {referenceFrame,indices}=settings.drift;
      const current=StructureSelection.remap(referenceFrame,frame,indices);
      frame=StructureGeometry.removeDrift(frame,referenceFrame,current,indices);
    }
    return settings.wrap?StructureGeometry.wrap(frame):frame;
  }
  function build(frame,settings) {
    const display=cloneDisplay(settings.display);
    if(settings.sourceFrame!==frame)display.transition(settings.sourceFrame,frame);
    return Object.freeze({frame:transform(frame,settings),options:Object.freeze({...settings,display})});
  }
  const api={settings,build,transform,lighting};
  if(typeof module!=='undefined'&&module.exports)module.exports=api;else root.StructureScene=api;
})(typeof window!=='undefined'?window:globalThis);
