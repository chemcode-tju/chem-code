import {captureMaps,mountMaps} from './plan-map.mjs';
import {resultFigures} from './result-figures.mjs';
import {escapeHtml as esc} from './core.mjs';
import {createStructureBridge} from './structure-links.mjs';
import {describeStep, planSteps, workflowGroups, stepRows} from './workflow-plan.mjs';
import {generalRows,nodeStatus} from './general-plan.mjs';
import {kindName, taskLabel} from './plan-layout.mjs';
import {t} from './i18n.mjs';

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
  const host=screen.parentElement, dialog=$('step-dialog');
  let overviewTop=0, overviewMaps=new Map(), fromFull=false;
  function syncFull() {
    const label=t(dialog.open || document.fullscreenElement===screen ? '退出全屏':'全屏详情');
    $('step-full').title=label;$('step-full').setAttribute('aria-label',label);
    const full = dialog.open || document.fullscreenElement===screen;
    $('step-full').setAttribute('aria-pressed',String(full));
    $('step-full').querySelector('use').setAttribute('href','/assets/ui-icons.svg#i-'+(full?'shrink':'expand'));
  }
  function expandDetail(on) {
    if(on===dialog.open) return;
    if(on) {dialog.append(screen);dialog.showModal();}
    else {dialog.close();host.insertBefore(screen,dialog);}
    syncFull();
  }
  $('step-full').onclick=()=>{
    if(dialog.open) expandDetail(false);
    else if(document.fullscreenElement===screen) document.exitFullscreen().catch(()=>{});
    else if(screen.requestFullscreen) screen.requestFullscreen().catch(()=>expandDetail(true));
    else expandDetail(true);
  };
  document.addEventListener('fullscreenchange',syncFull);
  dialog.addEventListener('cancel',event=>{event.preventDefault();if(fromFull) close();else expandDetail(false);});
  function resetContent() {
    $('step-group-path').hidden=true;
    activeFile=''; info=null; bridge.reset(); $('step-viewer-shell').hidden=true;
    for (const id of ['step-note','step-error','step-progress','step-file-source','step-downloads','step-output','step-code','step-chart','step-figures','step-investigation-log']) $(id).replaceChildren();
    for (const id of ['step-investigation','step-output-section','step-code-section']) $(id).hidden=true;
    for (const section of screen.querySelectorAll('.step-sections > details')) section.open=false;
  }
  function enter() {
    if(screen.hidden) {
      overviewTop=screen.parentElement.scrollTop;
      overviewMaps=captureMaps($('plan-overview'),{suspend:true});
      fromFull=[...overviewMaps.values()].some(view=>view.full);
      $('plan-overview').hidden=true; screen.hidden=false;
      if(fromFull) expandDetail(true);
    }
    resetContent();
    screen.scrollTop=0;screen.parentElement.scrollTop=0;
  }
  function close() {
    if(screen.hidden) return;
    if(document.fullscreenElement===screen) document.exitFullscreen().catch(()=>{});
    current=null;resetContent();expandDetail(false);
    screen.hidden=true; $('plan-overview').hidden=false;
    mountMaps($('plan-overview'),overviewMaps);
    screen.parentElement.scrollTop=overviewTop;
  }
  function heading(title,kind,id,index,total,planName,purpose='') {
    $('step-plan-name').textContent=planName || ''; $('step-plan-name').title=planName || '';
    $('step-title').textContent=title;$('step-kind').textContent=t(kind);
    $('step-index').textContent=String(index+1).padStart(2,'0')+' / '+total;
    $('step-key').textContent=id;$('step-purpose').textContent=purpose;
  }
  function status(label,tone) {
    $('step-status').textContent=label;$('step-status').dataset.state=tone;
  }
  function relations(before,after) {
    const chip=n=>`<button type="button" data-related="${esc(n.id)}" title="${esc(n.id)}">${esc(taskLabel(n))}</button>`;
    const line=(label,nodes)=>`<div><h3>${t(label)} <small>${nodes.length}</small></h3><div class="relation-chips">${nodes.slice(0,3).map(chip).join('')}${nodes.length>3?`<details><summary>+ ${nodes.length-3}</summary><div class="relation-chips">${nodes.slice(3).map(chip).join('')}</div></details>`:''}</div></div>`;
    $('step-relations').innerHTML=(before.length?line('直接上游',before):'')+(after.length?line('直接下游',after):'');
    $('step-relations').hidden=!before.length && !after.length;
  }
  $('step-relations').onclick=event=>{
    const id=event.target.closest('[data-related]')?.dataset.related;
    if(id && current?.related) current.related(id);
  };
  function drawFiles() {
    const options=fileNames.filter(name=>info?.files[name]);
    $('step-files').innerHTML=options.map(name=>
      `<button type="button" data-step-file="${name}" aria-pressed="${name===activeFile}">${name}</button>`).join('') || '<span class="step-empty">'+t('暂无可查看的结构文件')+'</span>';
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
      if (!activeFile || sourceChanged) {
        const nextFile=next.files[activeFile]?activeFile:fileNames.find(name=>next.files[name]);
        if(nextFile && $('step-structures').open && !$('step-structures').hidden) selectFile(nextFile);
        else if(activeFile) {activeFile='';bridge.reset();$('step-viewer-shell').hidden=true;$('step-file-source').textContent='';}
      }
      else if (manual && activeFile!=='POSCAR') selectFile(activeFile);
      if (next.ionic_step!=null) rememberCount(view.id,view.key,next.ionic_step);
    } catch(error) {
      if(current===view) $('step-note').textContent=error.message;
    } finally { view.loading=false; }
  }
  function open(item,job,key,sessionPath) {
    const steps=planSteps(item), step=steps.find(s=>s.stage===key);
    if (!step) return;
    enter();
    const details=describeStep(item,job,step);
    const path=key==='single'?'POSCAR':key+'/POSCAR';
    current={id:item.preview_id,key,failed:details.tone==='failed',title:details.title,nsw:details.nsw,preview:item.files?.[path],
      related:id=>open(item,job,id,sessionPath),
      base:`${sessionPath}/plans/${encodeURIComponent(item.preview_id)}/steps/${encodeURIComponent(key)}`};
    screen.dataset.planId=job?.id || '';
    screen.dataset.nodeId=details.child?.id || '';
    screen.dispatchEvent(new Event('stepchange'));
    heading(details.title,key==='energies'?'数据分析':'VASP 计算',key,steps.indexOf(step),steps.length,item.structure?.formula);
    status(details.status,details.tone);
    const groups=workflowGroups(item), level=groups.findIndex(g=>g.some(n=>n.stage===key));
    const names=group=>(group || []).map(n=>({id:n.stage,title:describeStep(item,job,n).title}));
    relations(names(groups[level-1]),names(groups[level+1]));
    $('step-config').hidden=false;delete $('step-parameters').dataset.html;
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
  $('step-structures').addEventListener('toggle',()=>{
    if(!current || !$('step-structures').open || activeFile) return;
    const name=fileNames.find(name=>info?.files[name]);if(name) selectFile(name);
  });
  $('step-files').onclick=event=>{if(event.target.dataset.stepFile)selectFile(event.target.dataset.stepFile);};
  $('step-revise').onclick=()=>{if(current)reviseStep(current.id,current.title,current.revision);};
  setInterval(()=>{if(current && (!info?.terminal || current.failed || info?.investigations?.some(row=>!row.terminal)) && !document.hidden && screen.getClientRects().length)refresh();},20000);
  function drawGeneral(plan,node) {
    const state=plan.runtime[node.id];
    const path=$('step-group-path');
    path.textContent=(node.group_path || []).join(' › ');path.hidden=!path.textContent;
    status(nodeStatus(plan,node),state.status);
    const config=$('step-parameters'), html=generalRows(plan,node);
    if (config.dataset.html!==html) {config.innerHTML=html;config.dataset.html=html;}
    $('step-revise').hidden=plan.status!=='draft';
    $('step-config').hidden=!html && $('step-revise').hidden;
    $('step-code-section').hidden=!(node.code || node.job?.script); $('step-code').textContent=node.code || node.job?.script || '';
    const files=state.outputs?.files || [], result=['analysis','job'].includes(node.kind) && state.outputs?.result;
    $('step-output-section').hidden=!result && !files.length;
    $('step-output').textContent=result?JSON.stringify(result,null,2):'';
    const base = current.sessionPath + '/workflows/' + encodeURIComponent(plan.id) + '/artifacts/' + encodeURIComponent(node.id);
    const figures=resultFigures(base,files,node.title);
    if($('step-figures').innerHTML!==figures) $('step-figures').innerHTML=figures;
    $('step-downloads').innerHTML=files.map(name=>`<a download href="${esc(base+'/'+name.split('/').map(encodeURIComponent).join('/'))}">${esc(name)} <span aria-hidden="true">↓</span></a>`).join('');
    $('step-error').textContent=state.error || state.sync_error || '';
    current.plan=plan;
  }
  function openGeneral(plan,node,sessionPath) {
    enter();
    current={id:plan.id,key:node.id,general:true,revision:plan.revision,title:node.title,sessionPath,
      failed:plan.runtime[node.id].status==='failed',nsw:node.effective?.incar?.NSW,
      related:id=>{const p=current.plan,n=p.nodes.find(n=>n.id===id);if(n) openGeneral(p,n,sessionPath);},
      base:`${sessionPath}/workflows/${plan.id}/steps/${node.id}`};
    heading(taskLabel(node),kindName(node.kind),node.id,plan.nodes.indexOf(node),plan.nodes.length,plan.title,node.purpose);
    screen.dataset.planId=plan.id;screen.dataset.nodeId=node.id;
    screen.dispatchEvent(new Event('stepchange'));
    const index=new Map(plan.nodes.map(n=>[n.id,n]));
    relations(node.depends_on.map(id=>index.get(id)).filter(Boolean),plan.nodes.filter(n=>n.depends_on.includes(node.id)));
    drawGeneral(plan,node);
    $('step-back').focus({preventScroll:true});
    $('step-energies').hidden=node.kind!=='vasp'; $('step-progress').hidden=node.kind!=='vasp';$('step-refresh').hidden=false;
    $('step-structures').hidden=['analysis','job'].includes(node.kind);
    $('step-config').open=true; $('step-structures').open=node.kind==='model';
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
    status(details.status,details.tone); $('step-parameters').innerHTML=stepRows(details.rows);
    $('step-revise').hidden=Boolean(job);
  }};
}
