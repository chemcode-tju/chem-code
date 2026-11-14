import {escapeHtml as esc} from './core.mjs';
import {t} from './i18n.mjs';
import {convergenceData,plotScale,formatTick,zoomRange,lineSegments,selectedIonicPoint} from './convergence-model.mjs';

const finite=Number.isFinite;
const baseLabels={energy:'E0',delta:'|ΔE|',force:'Fmax',temperature:'T',potential:'势能',total:'总能量',kinetic:'动能',pressure:'压力'};
const baseUnits={energy:'eV',delta:'eV',force:'eV/Å',temperature:'K',potential:'eV',total:'eV',kinetic:'eV',pressure:'bar'};
const number=(value,mode)=>finite(value)?(mode==='energy'?value.toFixed(6):value===0?'0':Math.abs(value)<.001?value.toExponential(2):value.toFixed(4)):'';
const icon=(name)=>`<svg class="icon" aria-hidden="true"><use href="/assets/ui-icons.svg#i-${name}"/></svg>`;
const button=(action,label,name)=>`<button type="button" data-action="${action}" title="${t(label)}" aria-label="${t(label)}">${icon(name)}</button>`;

export function createConvergenceCard(host,{onTrajectory=()=>{}}={}) {
  let data=convergenceData(),info={},mode='energy',range=[0,0],identity='',geometry=null,hover=null;
  let width=0,signature='',mounted=false,selectedStep=null;
  let labels={...baseLabels},units={...baseUnits};
  const title=()=>data.isMD?(data.monitor.engine+' · '+t('分子动力学')):t('VASP 离子步收敛');
  const stepLabel=()=>t(data.isMD?'MD 步':'离子步');
  const modes=()=>data.isMD?['temperature','potential','total','kinetic','energy','pressure'].filter(key=>data.points.some(p=>finite(p[key])) || key==='temperature'):['energy','delta',...(data.hasForce?['force']:[])];
  const trajectory=()=>data.monitor.trajectory_file || (info.files?.XDATCAR?'XDATCAR':null);
  const find=selector=>host.querySelector(selector);
  const colors=()=>{
    const style=getComputedStyle(host);
    return Object.fromEntries(['text','muted','border','accent','surface'].map(key=>[key,style.getPropertyValue('--'+key).trim()]));
  };
  function mount() {
    host.innerHTML=`<section class="convergence-card" aria-label="${esc(title())}">
      <div class="conv-heading"><h3>${esc(title())}</h3><span class="conv-status"></span></div>
      <div class="conv-progress"></div><dl class="conv-metrics"></dl>
      <div class="conv-toolbar"><div class="conv-modes" role="group" aria-label="${t(data.isMD?'MD 视图':'收敛视图')}">
        ${modes().map(key=>`<button type="button" data-mode="${key}">${esc(t(labels[key]))}${key==='delta'?' <small>log</small>':''}</button>`).join('')}</div>
        <div class="conv-actions"><span class="conv-selected-step"></span>${button('reset','重置缩放','reset')}${button('export','导出 SVG','save')}${button('trajectory','查看结构轨迹','timeline')}</div></div>
      <div class="conv-plot" tabindex="0" role="group" aria-label="${t(data.isMD?'MD 图：点击选择时间步，滚轮缩放，方向键查看数据':'收敛图：点击选择离子步，滚轮缩放，方向键查看数据')}"><svg></svg><div class="conv-tooltip" hidden></div></div>
      <div class="conv-range"><div class="conv-overview"><svg aria-hidden="true"></svg>
        <input type="range" min="0" step="1" data-bound="0" aria-label="${t('起始离子步')}">
        <input type="range" min="0" step="1" data-bound="1" aria-label="${t('结束离子步')}"></div>
        <div class="conv-range-labels"><span></span><button type="button" data-action="recent">${t('最近 20 步')}</button><span></span></div></div>
      <p class="conv-note"></p></section>`;
    const plot=find('.conv-plot');
    plot.addEventListener('pointermove',event=>{
      if(!geometry)return;
      const rect=plot.getBoundingClientRect(),px=event.clientX-rect.left;
      const {x,visible}=geometry;
      let lo=0,hi=visible.length-1;
      while(lo<hi){const mid=(lo+hi)>>1;if(x(visible[mid].step)<px)lo=mid+1;else hi=mid;}
      hover=visible[lo];
      if(lo>0 && Math.abs(x(visible[lo-1].step)-px)<Math.abs(x(hover.step)-px))hover=visible[lo-1];
      showHover();
    });
    plot.addEventListener('click',()=>{if(hover){selectedStep=hover.step;trajectoryLabel();showHover();}});
    plot.addEventListener('pointerleave',()=>{hover=null;showHover();});
    plot.addEventListener('wheel',event=>{
      if(!geometry || data.points.length<3)return;
      event.preventDefault();
      const rect=plot.getBoundingClientRect();
      range=zoomRange(range,data.points.length,Math.max(0,Math.min(1,(event.clientX-rect.left-geometry.left)/geometry.plotWidth)),event.deltaY>0?1.3:.75);
      hover=null;draw();
    },{passive:false});
    plot.addEventListener('keydown',event=>{
      if(!geometry)return;
      if(event.key==='Escape'){hover=null;selectedStep=null;trajectoryLabel();showHover();return;}
      if(event.key==='Enter'){event.preventDefault();openSelected();return;}
      if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;
      event.preventDefault();
      const points=geometry.visible,index=points.indexOf(selectedIonicPoint(data.points,range,selectedStep));
      hover=points[event.key==='Home'?0:event.key==='End'?points.length-1:Math.max(0,Math.min(points.length-1,index+(event.key==='ArrowLeft'?-1:1)))];
      selectedStep=hover.step;trajectoryLabel();showHover();
    });
    host.onclick=event=>{
      const selected=event.target.closest('[data-mode]')?.dataset.mode;
      if(selected){mode=selected;hover=null;draw();return;}
      const action=event.target.closest('[data-action]')?.dataset.action;
      if(action==='trajectory'){openSelected();return;}
      if(action==='export'){exportChart();return;}
      if(action==='reset')range=[0,Math.max(0,data.points.length-1)];
      else if(action==='recent')range=[Math.max(0,data.points.length-20),Math.max(0,data.points.length-1)];
      else return;
      hover=null;draw();
    };
    host.oninput=event=>{
      if(!event.target.matches('[data-bound]'))return;
      const side=Number(event.target.dataset.bound),value=Number(event.target.value);
      range[side]=side===0?Math.min(value,range[1]-1):Math.max(value,range[0]+1);
      hover=null;draw();
    };
    mounted=true;
  }
  function trajectoryLabel() {
    const point=selectedIonicPoint(data.points,range,selectedStep),button=find('[data-action="trajectory"]');
    button.hidden=!trajectory() || !info.files?.[trajectory()] || !point;
    find('.conv-selected-step').textContent=point?`${stepLabel()} ${point.step}`:'';
    const label=point?`${t('查看结构轨迹')} · ${stepLabel()} ${point.step}`:t('查看结构轨迹');
    button.title=label;button.setAttribute('aria-label',label);
  }
  function openSelected() {
    const point=selectedIonicPoint(data.points,range,selectedStep);
    if(point && trajectory() && info.files?.[trajectory()])onTrajectory(trajectory(),point.step);
  }
  function metrics() {
    find('.conv-heading h3').textContent=title();find('.convergence-card').setAttribute('aria-label',title());
    const last=data.points.at(-1),force=[...data.points].reverse().find(p=>finite(p.force));
    find('.conv-status').textContent=t(data.status);find('.conv-status').dataset.converged=String(!data.isMD && !!data.monitor.ionic_converged);
    const progress=find('.conv-progress'),nsw=data.isMD?data.monitor.total_steps:(data.monitor.nsw || info.nsw);
    progress.textContent=last?`${last.step}${nsw>0?' / '+nsw:''} ${t('步')}${data.isMD && finite(last.time_ps)?' · '+Number(last.time_ps.toPrecision(6))+' ps':''}`:'';
    progress.title=nsw>0?t(data.isMD?'计划 MD 步数':'NSW 为最大离子步数'):'';
    const rows=(data.isMD?['temperature','potential','total'].map(key=>[key,last?.[key]]):[['energy',last?.energy],['delta',last?.delta],['force',force?.force]]).filter(([,value])=>finite(value));
    find('.conv-metrics').innerHTML=rows.map(([key,value])=>`<div><dt>${esc(t(labels[key]))}${key==='force' && force.step!==last.step?` · ${force.step}`:''}</dt><dd>${number(value,key)}<small>${units[key]}</small></dd></div>`).join('');
    const desired=modes();
    if(Array.from(host.querySelectorAll('[data-mode]')).map(el=>el.dataset.mode).join()!==desired.join())
      find('.conv-modes').innerHTML=desired.map(key=>`<button type="button" data-mode="${key}">${esc(t(labels[key]))}${key==='delta'?' <small>log</small>':''}</button>`).join('');
    trajectoryLabel();
  }
  function draw() {
    if(!mounted)return;
    const all=data.points,visible=all.slice(range[0],range[1]+1);
    trajectoryLabel();
    if(!modes().includes(mode))mode=modes()[0];
    for(const control of host.querySelectorAll('[data-mode]'))control.setAttribute('aria-pressed',String(control.dataset.mode===mode));
    find('.conv-range').hidden=all.length<3;
    find('[data-action="export"]').disabled=!all.length;
    find('[data-action="reset"]').disabled=range[0]===0 && range[1]===all.length-1;
    const plot=find('.conv-plot'),svg=plot.querySelector('svg');
    const empty=!visible.some(p=>finite(p[mode]));
    let emptyLabel=plot.querySelector('.conv-empty');
    if(empty && !emptyLabel){emptyLabel=document.createElement('p');emptyLabel.className='conv-empty';plot.append(emptyLabel);}
    if(emptyLabel){emptyLabel.hidden=!empty;emptyLabel.textContent=t(info.error?'数据读取失败，请刷新重试':info.loading?'正在读取计算数据…':data.isMD?'尚无完整 MD 记录':mode==='delta'?'等待两个相邻离子步记录':'尚无完整离子步记录');}
    svg.style.display=empty?'none':'';plot.tabIndex=empty?-1:0;
    if(empty){svg.replaceChildren();geometry=null;find('.conv-tooltip').hidden=true;find('.conv-note').textContent='';return;}
    const c=colors(),scale=plotScale(visible,mode,data.monitor);
    width=Math.max(240,Math.round(plot.getBoundingClientRect().width || 340));
    const tickLabels=scale.ticks.map(v=>formatTick(v,scale));
    const measure=document.createElement('canvas').getContext('2d');measure.font='11px '+getComputedStyle(host).fontFamily;
    const left=Math.ceil(Math.max(...tickLabels.map(label=>measure.measureText(label).width),30))+12;
    const top=27,bottom=198,right=12,plotWidth=width-left-right;
    const start=visible[0].step,end=visible.at(-1).step;
    const timed=data.isMD && visible.every(p=>finite(p.time_ps)) && visible.at(-1).time_ps>visible[0].time_ps;
    const times=new Map(visible.map(p=>[p.step,p.time_ps]));
    const x=step=>start===end?left+plotWidth/2:left+(timed?(times.get(step)-visible[0].time_ps)/(visible.at(-1).time_ps-visible[0].time_ps):(step-start)/(end-start))*plotWidth;
    const y=value=>{const transformed=scale.transform(value);return transformed===null?null:bottom-(transformed-scale.min)/(scale.max-scale.min)*(bottom-top);};
    const text=(x,y,value,anchor='start',fill=c.muted)=>`<text x="${x}" y="${y}" text-anchor="${anchor}" fill="${fill}" font-size="11">${esc(value)}</text>`;
    const line=(x1,y1,x2,y2,color=c.border,dash='')=>`<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="${color}" stroke-width="1" ${dash?'stroke-dasharray="'+dash+'"':''}/>`;
    const grid=scale.ticks.map((v,i)=>{const py=bottom-(v-scale.min)/(scale.max-scale.min)*(bottom-top);return line(left,py,width-right,py)+text(left-9,py+4,tickLabels[i],'end');}).join('');
    const xTicks=[...new Set([start,...(end-start>2?[visible[Math.floor(visible.length/2)].step]:[]),end])];
    const guides=scale.guides.map(g=>line(left,y(g.value),width-right,y(g.value),c.muted,'3 4')+text(width-right-3,y(g.value)-5,g.label,'end')).join('');
    const last=visible.filter(p=>finite(p[mode])).at(-1);
    const zero=mode==='delta'?visible.filter(p=>p.delta===0):[];
    svg.setAttribute('viewBox',`0 0 ${width} 232`);svg.setAttribute('role','img');svg.setAttribute('aria-label',`${labels[mode]} · ${start}–${end} ${t('步')}`);
    svg.innerHTML=`<g font-family="${esc(getComputedStyle(host).fontFamily)}">${text(left,13,t(labels[mode])+' ('+units[mode]+')'+(scale.log?' · log₁₀':''))}${grid}${guides}
      <path d="${lineSegments(visible,mode,x,y,!!data.isMD)}" fill="none" stroke="${c.accent}" stroke-width="1.65" stroke-linejoin="round"/>
      ${last && y(last[mode])!==null?`<circle cx="${x(last.step)}" cy="${y(last[mode])}" r="3" fill="${c.accent}"/>`:''}
      ${last && mode==='delta' && last.delta===0?`<circle cx="${x(last.step)}" cy="${bottom+4}" r="3" fill="${c.surface}" stroke="${c.accent}"/>`:''}
      ${xTicks.map(v=>text(x(v),219,timed?String(Number(times.get(v).toPrecision(5))):String(v),v===start?'start':v===end?'end':'middle')).join('')}
      ${text(width-right,13,timed?'ps':stepLabel(),'end')}<g data-hover></g></g>`;
    geometry={x,y,visible,left,plotWidth,top,bottom,c,text,line};
    const notes=[];
    if(data.monitor.history_truncated)notes.push(t('显示日志尾部的完整记录'));
    if(data.isMD && data.monitor.source_file)notes.push(data.monitor.source_file);
    if(mode==='delta')notes.push(t('虚线为能量差参考值'));
    if(zero.length)notes.push(t('空心点为 0，不参与对数轴'));
    if(mode==='force') {
      notes.push(t(({free_atoms:'Fmax：活动原子的最大力范数',all_atoms:'Fmax：全部原子的最大力范数',raw_directional_constraints:'原始 Fmax，未投影方向约束',raw:'原始 Fmax，约束信息不可用'})[data.monitor.force_basis] || 'Fmax：全部原子的最大力范数'));
      if(scale.guides.length)notes.push(`|EDIFFG| = ${number(-data.monitor.ediffg,'force')} eV/Å`);
      if(data.monitor.force_history_truncated)notes.push(t('受力历史来自 OUTCAR 尾部'));
    }
    find('.conv-note').textContent=notes.join(' · ');
    drawRange(c);showHover();
  }
  function drawRange(c) {
    const all=data.points,count=all.length;
    if(count<3)return;
    const overview=find('.conv-overview'),w=Math.max(200,overview.getBoundingClientRect().width),scale=plotScale(all,mode,data.monitor);
    const x=step=>4+(step-all[0].step)/(all.at(-1).step-all[0].step)*(w-8),y=value=>scale.transform(value)===null?null:24-(scale.transform(value)-scale.min)/(scale.max-scale.min)*22;
    const a=4+range[0]/(count-1)*(w-8),b=4+range[1]/(count-1)*(w-8),svg=overview.querySelector('svg');
    svg.setAttribute('viewBox',`0 0 ${w} 26`);
    svg.innerHTML=`<rect x="${a}" y="0" width="${b-a}" height="26" fill="${c.border}"/><path d="${lineSegments(all,mode,x,y,!!data.isMD)}" fill="none" stroke="${c.muted}" stroke-width="1"/>`;
    for(const input of overview.querySelectorAll('input')) {input.max=String(count-1);input.value=String(range[Number(input.dataset.bound)]);input.setAttribute('aria-valuetext',String(all[Number(input.value)].step));}
    const ends=find('.conv-range-labels').querySelectorAll('span');ends[0].textContent=String(all[range[0]].step);ends[1].textContent=String(all[range[1]].step);
  }
  function showHover() {
    const tooltip=find('.conv-tooltip'),layer=find('[data-hover]');
    const point=hover || geometry?.visible.find(p=>p.step===selectedStep);
    if(!layer || !geometry || !point || !geometry.visible.includes(point)){if(layer)layer.replaceChildren();tooltip.hidden=true;return;}
    const {x,y,top,bottom,c,line}=geometry,px=x(point.step),py=finite(point[mode])?y(point[mode]):null;
    layer.innerHTML=line(px,top,px,bottom,c.muted,'2 3')+(py!==null?line(geometry.left,py,width-12,py,c.muted,'2 3')+`<circle cx="${px}" cy="${py}" r="4" fill="${c.accent}" stroke="${c.surface}" stroke-width="2"/>`:mode==='delta' && point.delta===0?`<circle cx="${px}" cy="${bottom+4}" r="4" fill="${c.surface}" stroke="${c.accent}"/>`:'');
    if(!hover){tooltip.hidden=true;return;}
    tooltip.innerHTML=`<strong>${stepLabel()} ${hover.step}${finite(hover.time_ps)?' · '+Number(hover.time_ps.toPrecision(6))+' ps':''}</strong>`+(data.isMD?['temperature','potential','total','kinetic','energy','pressure']:['energy','delta','force']).filter(key=>finite(hover[key])).map(key=>`<div><span>${esc(t(labels[key]))}</span><b>${number(hover[key],key)} ${units[key]}</b></div>`).join('');
    tooltip.hidden=false;
    tooltip.style.left=Math.max(0,Math.min(width-tooltip.offsetWidth,px>width/2?px-tooltip.offsetWidth-12:px+12))+'px';
    tooltip.style.top=Math.max(20,Math.min(205-tooltip.offsetHeight,(py ?? top)+8))+'px';
  }
  function exportChart() {
    const svg=find('.conv-plot svg');if(!svg || !geometry)return;
    const clone=svg.cloneNode(true);clone.querySelector('[data-hover]')?.remove();
    const c=colors(),metricRows=Array.from(find('.conv-metrics').children).map(e=>e.querySelector('dt').textContent+' = '+e.querySelector('dd').textContent);
    const note=find('.conv-note').textContent,characters=Math.max(16,Math.floor((width-20)/10));
    const noteRows=note?Array.from({length:Math.ceil(note.length/characters)},(_,i)=>note.slice(i*characters,(i+1)*characters)):[];
    const chartTop=37+metricRows.length*16,height=chartTop+244+noteRows.length*14;
    const output=`<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}"><rect width="100%" height="100%" fill="${c.surface}"/><g font-family="sans-serif" fill="${c.text}"><text x="10" y="20" font-size="13">${esc(data.monitor.engine || 'VASP')} · ${esc(t(data.status))} · ${esc(find('.conv-progress').textContent)}</text>${metricRows.map((row,i)=>`<text x="10" y="${40+i*16}" font-size="11">${esc(row)}</text>`).join('')}</g><g transform="translate(0 ${chartTop})">${clone.innerHTML}</g>${noteRows.map((row,i)=>`<text x="10" y="${chartTop+244+i*14}" font-family="sans-serif" font-size="10" fill="${c.muted}">${esc(row)}</text>`).join('')}</svg>`;
    const url=URL.createObjectURL(new Blob([output],{type:'image/svg+xml;charset=utf-8'})),link=document.createElement('a');
    link.href=url;link.download=`${(data.monitor.engine || 'vasp').toLowerCase()}-${mode}-${data.points[range[0]].step}-${data.points[range[1]].step}.svg`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  }
  const observer=new ResizeObserver(()=>{const next=Math.round(host.getBoundingClientRect().width);if(next>0 && next!==width){width=next;draw();}});observer.observe(host);
  const onLanguage=()=>{if(mounted){metrics();draw();}};window.addEventListener('languagechange',onLanguage);
  const themeObserver=new MutationObserver(()=>draw());themeObserver.observe(document.documentElement,{attributes:true,attributeFilter:['data-theme']});
  return {update(next,key='') {
    const incoming=convergenceData(next),newSignature=JSON.stringify([next.points,next.monitor,next.terminal,next.scheduler_state,next.files,next.nsw,next.loading,next.error,key]);
    if(newSignature===signature && mounted)return;
    const follow=range[1]===data.points.length-1,full=follow && range[0]===0,span=range[1]-range[0];
    const reset=key!==identity || incoming.points.length<data.points.length || incoming.points[0]?.step!==data.points[0]?.step;
    if(reset)selectedStep=null;
    const kindChanged=!!data.isMD!==!!incoming.isMD || data.monitor.engine!==incoming.monitor.engine;
    info=next;data=incoming;identity=key;signature=newSignature;
    labels={...baseLabels,...data.monitor.energy_labels};
    units={...baseUnits,...(data.isMD?{temperature:data.monitor.temperature_unit || 'K',potential:data.monitor.energy_unit,total:data.monitor.energy_unit,kinetic:data.monitor.energy_unit,energy:data.monitor.energy_unit,pressure:data.monitor.pressure_unit}: {})};
    if(kindChanged){mounted=false;mode=data.isMD?'temperature':'energy';}
    if(reset || full || !mounted)range=[0,Math.max(0,data.points.length-1)];
    else if(follow)range=[Math.max(0,data.points.length-1-span),Math.max(0,data.points.length-1)];
    if(!mounted)mount();hover=null;metrics();draw();
  },reset(){data=convergenceData();info={};identity='';signature='';range=[0,0];hover=null;selectedStep=null;geometry=null;mounted=false;host.replaceChildren();},destroy(){window.removeEventListener('languagechange',onLanguage);observer.disconnect();themeObserver.disconnect();host.replaceChildren();}};
}
