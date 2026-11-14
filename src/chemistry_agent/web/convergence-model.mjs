// Scientific data and scale logic shared by the monitor and regression tests.
const finite = value => typeof value === 'number' && Number.isFinite(value);
export function selectedIonicPoint(points,range,selectedStep=null) {
  const visible=points.slice(range[0],range[1]+1);
  return visible.find(point=>point.step===selectedStep) || visible.at(-1) || null;
}
export function convergenceData(info={}) {
  if(info.monitor?.kind==='md') {
    const monitor=info.monitor,points=[];
    for(const row of monitor.md_points || []) {
      if(!Number.isSafeInteger(row.step) || row.step<0)continue;
      if(points.length && row.step<=points.at(-1).step)points.length=0;
      points.push({...row});
    }
    const state=String(info.scheduler_state || '').toUpperCase();
    return {points,monitor,isMD:true,hasForce:false,terminal:!!info.terminal,
      status:info.terminal?'已结束':state==='PENDING'?'排队中':monitor.stage==='minimization'?'准备中':points.length?'进行中':'等待 MD 记录'};
  }
  let points=[];
  for(const row of info.points || []) {
    if(!Array.isArray(row) || !Number.isInteger(row[0]) || row[0]<1 || !finite(row[1]))continue;
    if(points.length && row[0]<=points.at(-1).step)points=[];
    const previous=points.at(-1);
    points.push({step:row[0],energy:row[1],delta:previous?.step===row[0]-1?Math.abs(row[1]-previous.energy):null});
  }
  const monitor=info.monitor || {}, byStep=new Map(points.map(p=>[p.step,p]));
  for(const [step,force] of monitor.force_points || []) {
    if(!Number.isInteger(step) || step<1 || !finite(force) || force<0)continue;
    if(points.length && !byStep.has(step))continue;
    if(!byStep.has(step))byStep.set(step,{step,energy:null,delta:null});
    byStep.get(step).force=force;
  }
  points=[...byStep.values()].sort((a,b)=>a.step-b.step);
  const state=String(info.scheduler_state || '').toUpperCase();
  const status=monitor.ionic_converged?'已收敛':info.terminal?'已结束':
    state==='PENDING'?'排队中':points.length || state==='RUNNING'?'进行中':'等待数据';
  return {points,monitor,status,hasForce:points.some(p=>finite(p.force)),terminal:!!info.terminal};
}

// Nice-step selection adapted from design-taste-frontend/assets/charts.js.
export function linearScale(values,{nonnegative=false}={}) {
  const numbers=values.filter(finite);
  if(!numbers.length)return {min:0,max:1,ticks:[0,.25,.5,.75,1]};
  let min=Infinity,max=-Infinity;
  for(const value of numbers){min=Math.min(min,value);max=Math.max(max,value);}
  const span=max===min?Math.max(Math.abs(max)*1e-5,1e-3):Math.max(max-min,Math.abs(max)*1e-12,1e-10),padding=span*.12;
  min-=padding;max+=padding;
  if(nonnegative)min=Math.max(0,min);
  const raw=(max-min)/4,mag=10**Math.floor(Math.log10(raw));
  const step=([1,2,2.5,5,10].map(m=>m*mag).find(s=>s>=raw) || 10*mag);
  const ticks=[];
  for(let value=Math.ceil(min/step)*step;value<=max+step*1e-8;value+=step)ticks.push(Number(value.toPrecision(14)));
  return {min,max,ticks,step};
}
export function plotScale(points,mode,monitor={}) {
  const values=points.map(p=>p[mode]).filter(finite),guides=[];
  if(mode==='delta') {
    const positive=values.filter(v=>v>0);
    for(const value of [1e-4,1e-5])guides.push({value,label:value===1e-4?'10⁻⁴':'10⁻⁵'});
    const all=[...positive,1e-4,1e-5].map(Math.log10);
    const min=Math.floor(Math.min(...all))-.18,max=Math.ceil(Math.max(...all))+.18;
    const stride=Math.max(1,Math.ceil((max-min)/5)),ticks=[];
    for(let value=Math.ceil(min);value<=max;value+=stride)ticks.push(value);
    return {min,max,ticks,guides,log:true,transform:value=>value>0?Math.log10(value):null};
  }
  if(mode==='temperature' && finite(monitor.target_temperature))
    guides.push({value:monitor.target_temperature,label:'T target'});
  if(mode==='force' && monitor.kind!=='md' && monitor.ediffg<0 && ['free_atoms','all_atoms'].includes(monitor.force_basis))
    guides.push({value:-monitor.ediffg,label:'|EDIFFG|'});
  const scale=linearScale([...values,...guides.map(g=>g.value)],{nonnegative:['force','temperature','kinetic'].includes(mode)});
  return {...scale,guides,log:false,transform:value=>value};
}
export function formatTick(value,scale) {
  if(scale.log)return '10'+String(Math.round(value)).split('').map(c=>({'-':'⁻','0':'⁰','1':'¹','2':'²','3':'³','4':'⁴','5':'⁵','6':'⁶','7':'⁷','8':'⁸','9':'⁹'}[c] || c)).join('');
  if(value!==0 && (Math.abs(value)<1e-5 || Math.abs(value)>=1e7))return value.toExponential(1);
  const digits=Math.max(0,Math.min(9,-Math.floor(Math.log10(scale.step || .01))+(String(scale.step).startsWith('2.5')?1:0)));
  return value.toFixed(digits);
}
export function zoomRange(range,count,anchor,factor) {
  if(count<2)return [0,Math.max(0,count-1)];
  const oldSpan=range[1]-range[0],span=Math.min(count-1,Math.max(1,Math.round(oldSpan*factor)));
  const start=Math.max(0,Math.min(count-1-span,Math.round(range[0]+oldSpan*anchor-span*anchor)));
  return [start,start+span];
}
export function lineSegments(points,mode,x,y,continuous=false) {
  let previous=null;
  return points.map(p=>{
    const value=p[mode];
    if(!finite(value) || y(value)===null){previous=null;return '';}
    const command=previous && (continuous || previous.step===p.step-1)?'L':'M';previous=p;
    return `${command}${x(p.step).toFixed(2)},${y(value).toFixed(2)}`;
  }).join(' ');
}
