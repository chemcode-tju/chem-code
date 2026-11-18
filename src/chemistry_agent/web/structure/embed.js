/* Same-origin parent bridge. A selection is applied only after its file loads. */
(() => {
  const parentWindow=window.parent,origin=window.location.origin;
  if(parentWindow===window || origin==='null' || new URLSearchParams(window.location.search).get('embed')!=='1')return;
  document.documentElement.classList.add('embedded-viewer');
  let activeRequest=null,activeGeneration=null,operation=0,trajectoryRequest;
  const reply=(type,fields={})=>parentWindow.postMessage({type,...fields},origin);
  window.addEventListener('structure-frame-change',event=>{
    if(activeGeneration!==null && event.detail.generation===activeGeneration)
      reply('chem:viewer-position',{requestId:activeRequest,...event.detail});
  });
  window.addEventListener('message',async event=>{
    if(event.source!==parentWindow || event.origin!==origin)return;
    const message=event.data;
    if (message?.type==='chem:viewer-visibility') {
      setEmbedVisible(message.visible === true);
      return;
    }
    if(!['chem:open-structure','chem:seek-structure'].includes(message?.type))return;
    const {requestId,selection,navigationId}=message;
    if(message.type==='chem:seek-structure' && (requestId!==activeRequest || activeGeneration===null))return;
    const ticket=++operation;
    try {
      if(message.type==='chem:open-structure') {
        const {file,trajectory}=message;
        trajectoryRequest?.abort();
        activeRequest=requestId;activeGeneration=null;
        let files;
        if (trajectory) {
          trajectoryRequest = new AbortController();
          files = [StructureBrowserTrajectory.open(trajectory, trajectoryRequest.signal),
            ...(message.companions || []).filter(item=>item instanceof File)];
        } else {
          if(!(file instanceof File))throw Error('请选择结构文件。');
          files=message.sequence?message.files:[file,...(message.companions || []).filter(item=>item instanceof File)];
          if(!Array.isArray(files)||!files.length||files.some(item=>!(item instanceof File)))throw Error('路径结构文件无效');
        }
        const opened=await openStructure(files,'auto',selection,{sequence:message.sequence===true,name:message.name});
        if(!opened || ticket!==operation)return;
        if(opened.error)throw Error(opened.error);
        activeGeneration=opened.generation;
        const position=opened;
        if(!position || ticket!==operation)return;
        reply('chem:viewer-loaded',{requestId,name:opened.name,...position});
      } else {
        if(requestId!==activeRequest || activeGeneration===null)return;
        const position=await seekStructure(selection,activeGeneration);
        if(position && ticket===operation)reply('chem:viewer-frame',{requestId,navigationId,...position});
      }
    } catch(error) {
      if(ticket===operation)reply('chem:viewer-error',{requestId,navigationId,message:error.message});
    }
  });
  reply('chem:viewer-ready');
})();
