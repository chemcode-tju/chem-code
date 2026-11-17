import {escapeHtml as esc} from './core.mjs';
import {linearScale,formatTick} from './convergence-model.mjs';

export function resultURL(base,job,key=null,file=null) {
  const query=new URLSearchParams({job_id:job});
  if(key!==null)query.set('key',key);
  if(file!==null)query.set('file',file);
  return base+'/results?'+query;
}
export function selectedPath(data,step) {
  return data?.snapshots?.find(row=>row.step===step) || data?.snapshots?.at(-1) || null;
}
export function profilePoints(snapshot) {
  return (snapshot?.images || []).map(image=>[image.index,image.relative_energy]);
}

export function resultTimestamp(value,locale='zh-CN',timeZone) {
  if(!value)return '';
  const date=new Date(value);
  if(!Number.isFinite(date.getTime()))return String(value);
  return new Intl.DateTimeFormat(locale,{year:'numeric',month:'2-digit',day:'2-digit',
    hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23',...(timeZone?{timeZone}:{})}).format(date);
}

export function nebChart(data,step,mode='profile') {
  const row=selectedPath(data,step),images=row?.images || [],history=data?.snapshots || [];
  const modes=[['profile','像间能量']];
  if(images.some(item=>Number.isFinite(item.force)))modes.push(['image-force','像间力']);
  if(images.some(item=>Number.isFinite(item.endpoint_reference?.atomic_fmax)))modes.push(['endpoint-force','端点原子力']);
  if(history.some(item=>Number.isFinite(item.force)))modes.push(['force-history','力收敛']);
  modes.push(['energy-history','能量峰值']);
  if(!modes.some(([key])=>key===mode))mode='profile';
  const endpoint=mode==='endpoint-force',force=endpoint||mode==='image-force'||mode==='force-history',path=endpoint||mode==='profile'||mode==='image-force';
  const points=endpoint?images.filter(i=>Number.isFinite(i.endpoint_reference?.atomic_fmax)).map(i=>[i.index,i.endpoint_reference.atomic_fmax]):mode==='profile'?profilePoints(row):mode==='image-force'?images.filter(i=>Number.isFinite(i.force)).map(i=>[i.index,i.force]):
    history.filter(r=>Number.isFinite(force?r.force:r.peak_energy)).map(r=>[r.step,force?r.force:r.peak_energy]);
  return {mode,modes,points,action:path?'image':'iteration',target:force&&!endpoint?data?.force_target:null,
    label:endpoint?'端点 OUTCAR · 原始全原子最大力':mode==='profile'?'路径相对能量':mode==='image-force'?`优化步 ${row.step} · 各中间像投影最大力`:mode==='force-history'?'路径最大投影力收敛':'路径相对能量峰值历史',
    x_label:path?'路径像':'路径优化步',y_label:endpoint?'原子 Fmax':mode==='image-force'?'像 Fmax':force?'路径 Fmax':'Δ'+(data?.energy_label || 'E'),unit:force?'eV/Å':'eV'};
}
// Path history and image profiles have different selections and physical meanings.
export function historyImage(row,mode) {
  if(!row)return null;
  if(mode==='force-history')return row.images.find(item=>Number.isFinite(item.force) &&
    Number.isFinite(row.force) && Math.abs(item.force-row.force)<=1e-9*Math.max(1,Math.abs(row.force)))?.index ?? null;
  if(mode==='energy-history')return row.images.find(item=>Number.isFinite(item.relative_energy) &&
    Math.abs(item.relative_energy-row.peak_energy)<=1e-9*Math.max(1,Math.abs(row.peak_energy)))?.index ?? null;
  return null;
}
export function nebSelection(data,step,image,mode) {
  const row=selectedPath(data,step);if(!row)return '';
  const prefix=`优化步 ${row.step}`,selected=row.images.find(item=>item.index===image);
  if(mode==='force-history'||mode==='energy-history') {
    const value=mode==='force-history'?row.force:row.peak_energy;
    const label=mode==='force-history'?'路径最大投影力':'路径相对能量峰值';
    const unit=mode==='force-history'?'eV/Å':'eV',index=historyImage(row,mode);
    return `${prefix} · ${label} ${Number.isFinite(value)?value.toFixed(4)+' '+unit:'暂无记录'}${index!==null?' · 对应像 '+index:''}`;
  }
  if(!selected)return prefix;
  if(mode==='image-force')return `${prefix} · 像 ${image} · 投影最大力 ${Number.isFinite(selected.force)?selected.force.toFixed(4)+' eV/Å':'无记录'}`;
  if(mode==='endpoint-force') {
    const ref=selected.endpoint_reference;
    return `${prefix} · 像 ${image} · 端点原始全原子最大力 ${ref?ref.atomic_fmax.toFixed(4)+' eV/Å':'无记录'}`;
  }
  return `${prefix} · 像 ${image} · ${data.energy_label || 'E'} ${selected.energy.toFixed(6)} eV · ΔE ${selected.relative_energy.toFixed(4)} eV`;
}
const icon=name=>`<svg class="icon" aria-hidden="true"><use href="/assets/ui-icons.svg#i-${name}"/></svg>`;

function chart({label,x_label,y_label,unit,points},action='',selected=null,target=null,width=480) {
  if(!points.length)return `<p class="result-empty">${esc(label)} · 暂无完整记录</p>`;
  // Lay out labels at the actual panel width instead of shrinking all SVG text on narrow panes.
  const height=Math.max(240,Math.min(320,width*.48)),right=width-24,bottom=height-46;
  const xs=linearScale(points.map(p=>p[0])),ys=linearScale([...points.map(p=>p[1]),...(Number.isFinite(target)?[target]:[])]);
  const x=value=>64+(value-xs.min)/(xs.max-xs.min)*(right-64);
  const y=value=>bottom-(value-ys.min)/(ys.max-ys.min)*(bottom-36);
  const line=points.map(([a,b],i)=>`${i && (!action || a===points[i-1][0]+1)?'L':'M'}${x(a)},${y(b)}`).join(' ');
  return `<figure class="result-chart"><figcaption>${esc(label)}</figcaption><svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${esc(label)}">
    <text x="64" y="17" class="result-axis-title">${esc(y_label)}${unit?' / '+esc(unit):''}</text>
    ${ys.ticks.map(v=>`<path class="result-grid" d="M64 ${y(v)}H${right}"/><text x="56" y="${y(v)+4}" text-anchor="end">${esc(formatTick(v,ys))}</text>`).join('')}
    ${points.filter((p,i)=>i===0||i===points.length-1||points.length<9||i%Math.ceil(points.length/5)===0).map(([a])=>`<text x="${x(a)}" y="${bottom+20}" text-anchor="middle">${a}</text>`).join('')}
    <text x="${(64+right)/2}" y="${height-6}" text-anchor="middle">${esc(x_label)}</text>
    ${Number.isFinite(target)?`<path class="result-target" d="M64 ${y(target)}H${right}"/><text x="${right-3}" y="${y(target)-5}" text-anchor="end">目标 ${target}</text>`:''}
    <path class="result-line" d="${line}"/>
    ${points.map(([a,b],i)=>`<circle cx="${x(a)}" cy="${y(b)}" r="${a===selected?5:3}" class="${a===selected?'selected':''}" ${action?`data-${action}="${a}" tabindex="0" role="button" aria-label="${esc(x_label)} ${a}, ${b.toFixed(6)} ${esc(unit)}"`:''}><title>${esc(x_label)} ${a}: ${b.toFixed(6)} ${esc(unit)}</title></circle>`).join('')}
  </svg></figure>`;
}

export function createRegisteredResults(host,{api,openStructure,onStructures=()=>{},onReset=()=>{}}) {
  let context=null,data=null,key=null,step=null,image=null,mode='profile';
  let tail=true,generation=0,opened=false,plotWidth=480;
  function reset() {generation++;context=null;data=null;key=null;step=null;tail=true;image=null;mode='profile';opened=false;onReset();host.hidden=true;host.replaceChildren();onStructures([]);}
  async function showStructure(index,scroll=false) {
    if(!context || !data)return;
    const row=data.kind==='neb'?selectedPath(data,step)?.images.find(item=>item.index===index):data.structures[index];
    if(!row)return;
    image=index;opened=true;render();
    const url=resultURL(context.base,context.job,key,row.path);
    openStructure({name:row.path.split('/').at(-1),url,
      label:data.kind==='neb'?`路径优化步 ${step} · 像 ${index} · ${data.energy_label} ${row.energy.toFixed(6)} eV`:row.label,
      identity:context.job+':'+key+':'+row.path,scroll,
      ...(data.kind==='neb'?{sequence:{id:context.job+':'+key+':'+step,url:resultURL(context.base,context.job,key)+'&path_step='+step,step,frameIndex:selectedPath(data,step).images.findIndex(item=>item.index===index)}}:{})});
  }
  function render() {
    if(!context)return;
    host.hidden=false;
    // Keep the viewer outside the refreshed chart fragment. Rebuilding its iframe
    // on image/iteration changes discards playback state and triggers reloads.
    if(!host.querySelector('.registered-result-content'))host.innerHTML=`<summary></summary><div class="step-section-body"><div class="registered-result-content"></div><div class="result-viewer-slot"></div></div>`;
    const content=host.querySelector('.registered-result-content');
    plotWidth=Math.max(240,Math.round(content.clientWidth || host.clientWidth || 480));
    const active=host.contains(document.activeElement)?document.activeElement:null;
    const focusAttribute=active && ['data-dataset','data-path-step','data-follow','data-neb-mode','data-open-image','data-image','data-iteration'].find(name=>active.hasAttribute(name));
    const focusValue=focusAttribute?active.getAttribute(focusAttribute):null;
    const focusTag=active?.tagName;
    const row=selectedPath(data,step);
    const plot=row?nebChart(data,step,mode):null;if(plot)mode=plot.mode;
    if(row && !row.images.some(item=>item.index===image))image=row.images[0]?.index ?? null;
    const selected=row?.images.find(item=>item.index===image);
    if(row)step=row.step;
    onStructures(row?row.images.map(item=>({...item,step:row.step,selected:item.index===image})):[]);
    const options=context.datasets.map(item=>`<option value="${esc(item.key)}" ${item.key===key?'selected':''}>${esc(item.title)}</option>`).join('');
    const controls=data?.kind==='neb'?`<label>路径优化步 <select data-path-step aria-label="路径优化步">${data.snapshots.map(item=>`<option value="${item.step}" ${item.step===step?'selected':''}>${item.step}</option>`).join('')}</select></label><label class="result-follow"><input type="checkbox" data-follow ${tail?'checked':''}>最新</label>`:'';
    const structures=row?row.images.map(item=>`<button type="button" data-image="${item.index}" aria-pressed="${image===item.index}" aria-label="像 ${item.index}" title="像 ${item.index} · ${esc(item.path)}">${String(item.index).padStart(2,'0')}</button>`).join(''):(data?.structures || []).map((item,i)=>`<button type="button" data-image="${i}">${esc(item.label || item.path)}</button>`).join('');
    host.querySelector(':scope > summary').innerHTML=`${icon('compare')}${data?.kind==='neb'?'NEB 路径与收敛':'计算结果'}`;
    content.innerHTML=`<div class="result-controls">${context.datasets.length>1?`<select data-dataset aria-label="结果数据集">${options}</select>`:''}${controls}</div>
      ${data?.stale?`<p class="result-note result-warning" role="status"><span>读取失败，正显示缓存结果</span>${data.observed_at?`（<time datetime="${esc(data.observed_at)}" title="${esc(data.observed_at)}">${esc(resultTimestamp(data.observed_at))}</time>）`:''}</p>`:''}
      ${row?`<dl class="conv-metrics"><div><dt>路径像</dt><dd>${row.images.length}</dd></div><div><dt>相对能量峰值</dt><dd>${row.peak_energy.toFixed(4)}<small>eV</small></dd></div>${Number.isFinite(row.force)?`<div><dt>NEB 投影 Fmax</dt><dd>${row.force.toFixed(4)}<small>eV/Å</small></dd></div>`:''}</dl>
      <div class="conv-toolbar"><div class="conv-modes" role="group" aria-label="NEB 视图">${plot.modes.map(([key,label])=>`<button type="button" data-neb-mode="${key}" aria-pressed="${key===mode}">${label}</button>`).join('')}</div></div>`:`<div class="step-file-options">${structures}</div>`}
      <div class="result-plots">${row?chart(plot,plot.action,plot.action==='image'?image:step,plot.target,plotWidth):
        (data?.series || []).map(s=>chart(s,'',null,null,plotWidth)).join('') || '<p class="result-empty">等待完整结果快照</p>'}</div>
      ${row?`<p class="result-selection">${esc(nebSelection(data,step,image,mode))}</p><div class="result-images" role="group" aria-label="路径像">${structures}${selected?`<a href="${esc(resultURL(context.base,context.job,key,selected.path))}" download="${esc(selected.path.split('/').at(-1))}" title="下载所选像结构" aria-label="下载所选像结构">${icon('download')}</a>`:''}</div>${plot.action==='image'&&selected?.endpoint_reference?`<details class="result-reference"><summary>端点参考</summary><p>TOTEN ${selected.endpoint_reference.energy.toFixed(6)} eV · 原始全原子 Fmax ${selected.endpoint_reference.atomic_fmax.toFixed(4)} eV/Å</p><p>${esc(selected.endpoint_reference.source)}</p></details>`:''}`:''}
      ${data?.skipped?.length?`<details class="result-note"><summary>${data.skipped.length} 个快照暂未读取</summary>${data.skipped.map(s=>`<p>${esc(s.path)}: ${esc(s.reason)}</p>`).join('')}</details>`:''}
      ${data?.observed_at&&!data.stale?`<p class="result-note result-updated">更新于 <time datetime="${esc(data.observed_at)}" title="${esc(data.observed_at)}">${esc(resultTimestamp(data.observed_at))}</time></p>`:''}`;
    if(focusAttribute) [...host.querySelectorAll(`[${focusAttribute}]`)].find(element=>element.tagName===focusTag && element.getAttribute(focusAttribute)===focusValue)?.focus({preventScroll:true});
  }
  if(typeof ResizeObserver!=='undefined')new ResizeObserver(()=>{
    const width=host.querySelector('.registered-result-content')?.clientWidth;
    if(context && !host.hidden && width>0 && Math.abs(width-plotWidth)>=1)render();
  }).observe(host);
  async function load() {
    const ticket=++generation,ctx=context,chosen=key;
    try {
      const next=await api(resultURL(ctx.base,ctx.job,chosen));
      if(ticket!==generation || context!==ctx)return;
      const oldStep=step;
      data=next;if(tail || !next.snapshots?.some(row=>row.step===step))step=next.snapshots?.at(-1)?.step ?? null;
      if(data.available===false){host.hidden=true;return;}
      render();
      if(opened && tail && step!==oldStep)showStructure(image,false);
    } catch(error) {
      if(ticket===generation){render();const note=document.createElement('p');note.className='result-note';note.textContent=error.message;host.append(note);}
    }
  }
  async function refresh(base,job) {
    if(!job){reset();return;}
    const id=base+':'+job;
    if(context?.id!==id){reset();context={id,base,job,datasets:[]};}
    const ctx=context,ticket=generation;
    try {
      const listing=await api(resultURL(base,job));
      if(context!==ctx || ticket!==generation)return;
      ctx.datasets=listing.datasets;
      if(!ctx.datasets.length){host.hidden=true;return;}
      if(!ctx.datasets.some(item=>item.key===key))key=ctx.datasets[0].key;
      await load();
      return data?.available===false?null:data;
    } catch(error) {if(context===ctx && data){render();}}
  }
  host.addEventListener('change',event=>{
    if(event.target.matches('[data-dataset]')){key=event.target.value;data=null;step=null;tail=true;opened=false;image=null;mode='profile';onReset();onStructures([]);load();}
    if(event.target.matches('[data-path-step]')){step=Number(event.target.value);tail=false;render();if(opened)showStructure(image,false);}
    if(event.target.matches('[data-follow]')){tail=event.target.checked;if(tail){step=data?.snapshots?.at(-1)?.step;render();if(opened)showStructure(image,false);}}
  });
  function activate(event) {
    const tab=event.target.closest('[data-neb-mode]');
    if(tab){mode=tab.dataset.nebMode;render();return;}
    if(event.target.closest('[data-open-image]')){showStructure(image);return;}
    const item=event.target.closest('[data-image],[data-iteration]');if(!item)return;
    tail=false;
    if(item.hasAttribute('data-iteration')){step=Number(item.dataset.iteration);tail=false;const matched=historyImage(selectedPath(data,step),mode);if(matched!==null)image=matched;}
    if(item.hasAttribute('data-image'))image=Number(item.dataset.image);
    if(item.hasAttribute('data-image') || opened)showStructure(image,false);
    else render();
  }
  host.addEventListener('click',activate);
  host.addEventListener('keydown',event=>{if(['Enter',' '].includes(event.key)&&event.target.matches('circle')){event.preventDefault();activate(event);}});
  return {refresh,reset,isStale:()=>Boolean(data?.stale),selectImage:showStructure,reopenSelection(){if(opened)showStructure(image,false);},viewerSlot:()=>host.querySelector('.result-viewer-slot'),syncFrame(frameIndex){
    const row=selectedPath(data,step),item=row?.images[frameIndex];
    if(!item)return null;image=item.index;render();
    return {name:item.path.split('/').at(-1),url:resultURL(context.base,context.job,key,item.path),label:`路径优化步 ${step} · 像 ${image} · ${data.energy_label} ${item.energy.toFixed(6)} eV`};
  },clearSelection(){opened=false;}};
}
