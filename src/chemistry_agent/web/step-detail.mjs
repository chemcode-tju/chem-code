import {escapeHtml as esc} from './core.mjs';
import {createStructureBridge} from './structure-links.mjs';
import {describeStep, planSteps, stepRows} from './workflow-plan.mjs';
import {generalRows,nodeStatus} from './general-plan.mjs';

const fileNames = ['POSCAR', 'CONTCAR', 'XDATCAR'];

export function energyCurve(points) {
  if (!points.length) return '<p>尚无完整离子步能量记录。</p>';
  const energies = points.map(row => row[1]);
  const high = energies.reduce((a,b) => Math.max(a,b));
  const low = energies.reduce((a,b) => Math.min(a,b));
  const start = points[0][0], end = points.at(-1)[0];
  const x = step => 63 + 226*(step-start)/Math.max(1,end-start);
  const y = energy => high===low ? 66 : 106 - 83*(energy-low)/(high-low);
  const line = points.map(([step,energy]) => x(step).toFixed(2)+','+y(energy).toFixed(2)).join(' ');
  const last = points.at(-1);
  return `<svg viewBox="0 0 304 143" role="img" aria-label="离子步与体系总能量 E0"><text x="3" y="12">E0 (eV)</text>
    <path class="curve-axis" d="M62 20V110H294"/><text x="58" y="28" text-anchor="end">${high.toFixed(4)}</text>
    ${high!==low?`<text x="58" y="108" text-anchor="end">${low.toFixed(4)}</text>`:''}
    <polyline points="${line}"/><circle cx="${x(last[0])}" cy="${y(last[1])}" r="3"/>
    <text x="63" y="126">${start}</text>${end!==start?`<text x="288" y="126" text-anchor="end">${end}</text>`:''}
    <text x="165" y="139">离子步</text></svg><p>第 ${end} 步 · E0 = ${last[1]} eV</p>`;
}

export function createStepDetail({api,reviseStep,rememberCount}) {
  const $ = id => document.getElementById(id);
  const screen = $('step-screen'), frame = $('step-viewer-frame');
  let current = null, activeFile = '', info = null;
  const bridge = createStructureBridge({frame,toast:message => { $('step-note').textContent=message; },selectTab() {
    $('step-viewer-shell').hidden=false;
    if (!frame.hasAttribute('src')) frame.src='/structures?embed=1';
  }});
  function close() {
    current=null; activeFile=''; info=null; bridge.reset();
    screen.hidden=true; $('plan-overview').hidden=false; $('step-viewer-shell').hidden=true;
    $('step-code-section').hidden=true; $('step-output-section').hidden=true;
  }
  function drawFiles() {
    $('step-files').innerHTML=fileNames.map(name=>
      `<button type="button" data-step-file="${name}" aria-pressed="${name===activeFile}" ${info?.files[name]?'':'disabled'}>${name}</button>`).join('');
  }
  function selectFile(name) {
    if (!current || !info?.files[name]) return;
    const source = info.files[name], view = current;
    activeFile=name; drawFiles(); $('step-note').textContent='';
    let caption = source;
    if (name === 'POSCAR' && (source === '方案预览' || source === '实际提交')) {
      if (view.key === 'surface') caption += source === '方案预览'
        ? ' · 切面示意。正式输入由收敛后的体相晶格生成'
        : ' · 由收敛后的体相晶格切面生成';
      if (['top','bridge','hollow'].includes(view.key)) caption += source === '方案预览'
        ? ' · 位点示意。洁净表面优化通过后，再用其末态放置 CO'
        : ' · 在优化后的洁净表面上放置 CO';
    }
    $('step-file-source').textContent=caption;
    bridge.reset();
    bridge.open(name,async () => {
      if (name==='POSCAR' && source==='方案预览' && view.preview) return new Blob([view.preview]);
      const response=await fetch(view.base+'/'+name,{cache:'no-store'});
      if (!response.ok) {
        const error=await response.json().catch(()=>({}));
        throw new Error(error.error?.message || '结构读取失败，请刷新重试。');
      }
      return new File([await response.blob()],response.headers.get('X-Structure-Name')||name);
    });
  }
  async function refresh(manual=false) {
    const view=current;
    if (!view || (!view.general && view.key==='energies') || view.loading) return;
    view.loading=true;
    try {
      const next = await api(view.base+'/info');
      if (current!==view) return;
      const sourceChanged=activeFile && info?.files[activeFile]!==next.files[activeFile];
      info=next;
      const entries=next.investigations || [];
      $('step-investigation').hidden=!entries.length;
      $('step-investigation-log').innerHTML=entries.map(row=>{
        const title=row.kind==='diagnostic'?'对照计算':'接续计算';
        const scf=row.electronic_converged===true?'电子已收敛':row.electronic_converged===false?'电子未收敛':'';
        const energy=Number.isFinite(row.energy_ev)?`E0 = ${row.energy_ev} eV`:'';
        return `<li><strong>${title}${row.pk?' #'+esc(String(row.pk)):''}</strong><p>${esc(row.purpose)}</p><small>${esc([row.status,scf,energy].filter(Boolean).join(' · '))}</small></li>`;
      }).join('');
      drawFiles();
      $('step-note').textContent=next.note || '';
      $('step-progress').textContent=next.ionic_step!=null
        ? `已完成 ${next.ionic_step} 个离子步${view.nsw>0?' · NSW 上限 '+view.nsw:''}`
        : next.pk ? '等待首个完整离子步记录' : '尚无离子步记录';
      $('step-chart').innerHTML=energyCurve(next.points);
      if (!activeFile || sourceChanged) selectFile(next.files[activeFile]?activeFile:next.files.POSCAR?'POSCAR':'CONTCAR');
      else if (manual && activeFile!=='POSCAR') selectFile(activeFile);
      if (next.ionic_step!=null) rememberCount(view.id,view.key,next.ionic_step);
    } catch(error) {
      if(current===view) $('step-note').textContent=error.message;
    } finally { view.loading=false; }
  }
  function open(item,job,key,sessionPath) {
    close();
    const step=planSteps(item).find(s=>s.stage===key);
    if (!step) return;
    const details=describeStep(item,job,step);
    const path=key==='single'?'POSCAR':key+'/POSCAR';
    current={id:item.preview_id,key,failed:details.tone==='failed',title:details.title,nsw:details.nsw,preview:item.files?.[path],
      base:`${sessionPath}/plans/${encodeURIComponent(item.preview_id)}/steps/${encodeURIComponent(key)}`};
    $('plan-overview').hidden=true; screen.hidden=false;
    $('step-title').textContent=details.title; $('step-status').textContent=details.status;
    $('step-parameters').innerHTML=stepRows(details.rows);
    $('step-revise').hidden=Boolean(job);
    const aggregate=key==='energies';
    for (const id of ['step-structures','step-energies','step-refresh','step-progress']) $(id).hidden=aggregate;
    $('step-parameters').closest('details').open=aggregate;
    $('step-structures').open=true; $('step-energies').open=false;
    $('step-investigation').hidden=true; $('step-investigation-log').replaceChildren();
    $('step-progress').textContent='读取离子步…'; $('step-note').textContent='';
    $('step-file-source').textContent=''; $('step-chart').replaceChildren();
    info={files:current.preview?{POSCAR:'方案预览'}:{},points:[]};
    drawFiles();
    if (!job && !step.reused_pk && current.preview) selectFile('POSCAR');
    screen.parentElement.scrollTop=0;
    $('step-back').focus({preventScroll:true});
    refresh();
  }
  $('step-back').onclick=close;
  $('step-refresh').onclick=()=>refresh(true);
  $('step-files').onclick=event=>{if(event.target.dataset.stepFile)selectFile(event.target.dataset.stepFile);};
  $('step-revise').onclick=()=>{if(current)reviseStep(current.id,current.title,current.revision);};
  setInterval(()=>{if(current && (!info?.terminal || current.failed || info?.investigations?.some(row=>!row.terminal)) && !document.hidden && screen.getClientRects().length)refresh();},20000);
  function drawGeneral(plan,node) {
    const state=plan.runtime[node.id];
    $('step-status').textContent=nodeStatus(plan,node);
    $('step-parameters').innerHTML=generalRows(plan,node);
    $('step-revise').hidden=plan.status!=='draft';
    $('step-code-section').hidden=!node.code; $('step-code').textContent=node.code || '';
    const result=node.kind==='analysis' && state.outputs?.result;
    $('step-output-section').hidden=!result; $('step-output').textContent=result?JSON.stringify(result,null,2):'';
    $('step-downloads').innerHTML=(state.outputs?.files || []).map(name=>`<p><a download href="${current.sessionPath}/workflows/${plan.id}/artifacts/${node.id}/${name.split('/').map(encodeURIComponent).join('/')}">${esc(name)}</a></p>`).join('');
    if(state.error || state.sync_error) $('step-note').textContent=state.error || state.sync_error;
  }
  function openGeneral(plan,node,sessionPath) {
    close();
    current={id:plan.id,key:node.id,general:true,revision:plan.revision,title:node.title,sessionPath,
      failed:plan.runtime[node.id].status==='failed',nsw:node.effective?.incar.NSW,
      base:`${sessionPath}/workflows/${plan.id}/steps/${node.id}`};
    $('plan-overview').hidden=true; screen.hidden=false; $('step-title').textContent=node.title;
    drawGeneral(plan,node);
    for(const id of ['step-structures','step-energies','step-refresh','step-progress']) $(id).hidden=false;
    $('step-energies').hidden=node.kind!=='vasp'; $('step-progress').hidden=node.kind!=='vasp';
    $('step-structures').hidden=node.kind==='analysis';
    $('step-parameters').closest('details').open=true; $('step-structures').open=true;
    $('step-progress').textContent=''; $('step-file-source').textContent=''; $('step-chart').replaceChildren();
    info={files:{},points:[]}; drawFiles(); refresh();
  }
  return {open,close,openGeneral,updateGeneral(plan) {
    if(!current?.general || current.id!==plan.id) return;
    const node=plan.nodes.find(n=>n.id===current.key);
    if(!node) {close();return;}
    current.revision=plan.revision; drawGeneral(plan,node);
  },update(item,job) {
    if (!current || current.id!==item.preview_id) return;
    const step=planSteps(item).find(s=>s.stage===current.key);
    if (!step) return;
    const details=describeStep(item,job,step);
    $('step-status').textContent=details.status; $('step-parameters').innerHTML=stepRows(details.rows);
    $('step-revise').hidden=Boolean(job);
  }};
}
