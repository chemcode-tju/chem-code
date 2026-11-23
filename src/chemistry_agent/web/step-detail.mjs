import {captureMaps,mountMaps} from './plan-map.mjs';
import {resultFigures} from './result-figures.mjs';
import {escapeHtml as esc} from './core.mjs';
import {createStructureBridge} from './structure-links.mjs';
import {describeStep, planSteps, workflowGroups} from './workflow-plan.mjs';
import {nodeStatus,graphLayers} from './general-plan.mjs';
import {workflowView} from './plan-presentation.mjs';
import {executionOptions} from './step-executions.mjs';
import {createTextEditor} from './step-editor.mjs';
import {kindName, taskLabel} from './plan-layout.mjs';
import {t} from './i18n.mjs';
import {createConvergenceCard} from './convergence-card.mjs';
import {createRegisteredResults,resultTimestamp} from './registered-results.mjs';
import {createTaskThread} from './task-thread.mjs';
import {attentionLabel,stepAttention} from './plan-attention.mjs';

const fileNames = ['POSCAR', 'CONTCAR', 'XDATCAR'];
const availableFiles=info=>[...new Set([...fileNames,...Object.keys(info?.files || {})])].filter(name=>info?.files?.[name]);

export function createStepDetail({api,rememberCount,onAttentionChange}) {
  const $ = id => document.getElementById(id);
  const screen = $('step-screen'), frame = $('step-viewer-frame');
  const taskThread = createTaskThread($('step-task-thread'), api, onAttentionChange);
  const pendingStep = $('step-attention'), feedback = $('step-attention-feedback');
  let current = null, activeFile = '', info = null, activeSnapshot = null, focusStep = null;
  const editors=new Map();
  let selectedParameter='',viewerPosition=null;
  const viewerShell=$('step-viewer-shell'),viewerHome=viewerShell.parentElement;
  const restoreViewer=()=>{if(viewerShell.parentElement!==viewerHome)viewerHome.append(viewerShell);};
  let registeredSelection=null,registeredFiles=[];
  const results=createRegisteredResults($('step-registered-results'),{api,onReset(){restoreViewer();registeredSelection=null;viewerShell.hidden=true;bridge.reset();},onStructures(files){registeredFiles=files;drawFiles();},openStructure({name,url,label,identity,scroll=false,sequence=null}) {
    const sameSequence=sequence && registeredSelection?.sequence?.id===sequence.id;
    registeredSelection={identity,label,url,name,sequence};activeFile='';activeSnapshot=null;
    const slot=results.viewerSlot();
    if(slot && viewerShell.parentElement!==slot)slot.append(viewerShell);
    $('step-registered-results').open=true;
    $('step-file-source').textContent=label;drawFiles();
    if(sameSequence && bridge.seek({frameIndex:sequence.frameIndex})) {
      if(scroll)viewerShell.scrollIntoView({block:'nearest',behavior:'smooth'});
      return;
    }
    bridge.reset();bridge.open(sequence?`NEB 路径 · 优化步 ${sequence.step}`:name,async()=>{
      if(sequence){
        const data=await api(sequence.url);
        if(data.stale && !results.isStale() && registeredSelection?.sequence?.id===sequence.id)$('step-note').textContent=`缓存路径 · ${resultTimestamp(data.observed_at)}`;
        if(data.kind!=='neb_sequence'||!data.files?.length)throw Error('尚无完整路径结构');
        return data.files.map(item=>new File([item.content],item.name));
      }
      const response=await fetch(url,{cache:'no-store'});
      if(!response.ok){const error=await response.json().catch(()=>({}));throw new Error(error.error?.message || '结构读取失败');}
      return new File([await response.blob()],name);
    },sequence?{sequence:true,selection:{frameIndex:sequence.frameIndex}}:{});
    if(scroll)viewerShell.scrollIntoView({block:'nearest',behavior:'smooth'});
  }});
  const convergence=createConvergenceCard($('step-chart'),{onTrajectory(name,ionicStep) {
    const incar=(info?.parameters?.INCAR || '').split('\n').map(line=>line.split(/[!#]/)[0]).join('\n');
    const stride=Number(incar.match(/\bNBLOCK\s*=\s*(\d+)/i)?.[1] || 1);
    focusStep=ionicStep;
    $('step-structures').open=true;selectFile(name,info?.monitor?.engine==='LAMMPS'?{simulationStep:ionicStep}:{ionicStep,stride});
  }});
  function editor(field,host,options={}) {
    if(!editors.has(field)) {
      const view=current;
      editors.set(field,createTextEditor(host,{label:field==='purpose'?'步骤说明':field==='script'?'提交脚本':field,...options,
        save:async(content,revision)=>{
          const plan=await api(view.base+'/edit',{method:'POST',body:JSON.stringify({field,content,expected_revision:revision})});
          if(current!==view)return;
          current.revision=plan.revision;
          if(info?.editing)info.editing.revision=plan.revision;
          if(field==='script' && info)info.script=content;
          if(['INCAR','KPOINTS'].includes(field) && info)info.parameters={...info.parameters,[field]:content};
          drawGeneral(plan,plan.nodes.find(n=>n.id===view.key));
        },
      }));
    }
    return editors.get(field);
  }
  function textDetails({purpose='',parameters={},script='',editing={},codeTitle='提交脚本'}) {
    const policy={revision:current?.revision,...editing};
    editor('purpose',$('step-purpose'),{prose:true}).update({text:purpose,editable:policy.purpose,revision:policy.revision,reason:policy.reason});
    const parameterNames=Object.keys(parameters),parameterHost=$('step-actual-parameters');
    $('step-config').hidden=!parameterNames.length;
    if(!parameterNames.includes(selectedParameter))selectedParameter=parameterNames[0] || '';
    let tabs=parameterHost.querySelector('[role=tablist]');
    if(!tabs){tabs=document.createElement('div');tabs.className='step-parameter-tabs';tabs.setAttribute('role','tablist');tabs.setAttribute('aria-label','参数文件');parameterHost.append(tabs);}
    const fileKey=JSON.stringify(parameterNames);
    if(tabs.dataset.files!==fileKey) {
      tabs.dataset.files=fileKey;
      tabs.innerHTML=parameterNames.map((name,index)=>`<button type="button" role="tab" id="step-parameter-tab-${index}" data-parameter-tab="${esc(name)}" aria-controls="step-parameter-panel-${index}" aria-selected="${name===selectedParameter}" tabindex="${name===selectedParameter?0:-1}">${esc(name)}</button>`).join('');
    }
    for(const [name,text] of Object.entries(parameters)) {
      if(!editors.has(name)) {
        const panel=document.createElement('div');panel.dataset.parameterFile=name;panel.setAttribute('role','tabpanel');parameterHost.append(panel);editor(name,panel);
      }
      editors.get(name).update({text,editable:policy.files && ['INCAR','KPOINTS'].includes(name),revision:policy.revision,reason:policy.reason});
    }
    for(const panel of parameterHost.querySelectorAll('[data-parameter-file]')) {
      const index=parameterNames.indexOf(panel.dataset.parameterFile);
      panel.hidden=panel.dataset.parameterFile!==selectedParameter || index<0;
      panel.id='step-parameter-panel-'+index;panel.setAttribute('aria-labelledby','step-parameter-tab-'+index);
    }
    selectParameter(selectedParameter);
    $('step-code-title').textContent=codeTitle;
    $('step-code-section').hidden=!script && !policy.files;
    editor('script',$('step-code')).update({text:script,editable:policy.files,revision:policy.revision,reason:policy.reason});
  }
  function selectParameter(name,focus=false) {
    selectedParameter=name;
    for(const button of $('step-actual-parameters').querySelectorAll('[role=tab]')) {
      const active=button.dataset.parameterTab===name;
      button.setAttribute('aria-selected',String(active));button.tabIndex=active?0:-1;if(active&&focus)button.focus();
    }
    for(const panel of $('step-actual-parameters').querySelectorAll('[data-parameter-file]'))panel.hidden=panel.dataset.parameterFile!==name;
  }
  $('step-actual-parameters').addEventListener('click',event=>{const tab=event.target.closest('[data-parameter-tab]');if(tab)selectParameter(tab.dataset.parameterTab);});
  $('step-actual-parameters').addEventListener('keydown',event=>{
    if(!event.target.matches('[role=tab]') || !['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;
    const tabs=[...$('step-actual-parameters').querySelectorAll('[role=tab]')],index=tabs.indexOf(event.target);
    const next=event.key==='Home'?0:event.key==='End'?tabs.length-1:(index+(event.key==='ArrowRight'?1:-1)+tabs.length)%tabs.length;
    event.preventDefault();selectParameter(tabs[next].dataset.parameterTab,true);
  });
  const bridge = createStructureBridge({frame,toast:message => { focusStep=null;$('step-note').textContent=message; },onFrame(position) {
    viewerPosition=position;
    if(registeredSelection){
      if(registeredSelection.sequence && Number.isInteger(position.frameIndex)){
        const selected=results.syncFrame(position.frameIndex);
        if(selected){Object.assign(registeredSelection,selected);$('step-file-source').textContent=selected.label;drawFiles();}
      }
      return;
    }
    const step=position.simulationStep ?? position.ionicStep;
    if(Number.isInteger(step))
      $('step-file-source').textContent=(info?.files[activeFile] || '')+' · '+t(info?.monitor?.kind==='md'?'MD 步':'离子步')+' '+step;
    if(focusStep!==null && Number.isInteger(step)) {
      if(step!==focusStep)$('step-note').textContent=`${t('所选 MD 步')} ${focusStep} · ${t('最近保存的轨迹步')} ${step}`;
      focusStep=null;$('step-structures').scrollIntoView({block:'start',behavior:'smooth'});
    }
  },selectTab() {
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
    // Moving an iframe between the dock and dialog reloads its document. Replay
    // the selected structure after that move instead of leaving an empty viewer.
    if(registeredSelection) {
      registeredSelection=null;bridge.reset();results.reopenSelection();
    } else if(activeFile && current) {
      const name=activeFile,selection=viewerPosition?{frameIndex:viewerPosition.frameIndex}:null;
      activeFile='';selectFile(name,selection);
    }
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
    taskThread.update(null, null);
    pendingStep.hidden=feedback.hidden=true;
    pendingStep.replaceChildren();
    feedback.textContent='';
    results.reset();registeredSelection=null;
    convergence.reset();
    editors.clear();selectedParameter='';$('step-purpose').replaceChildren();
    $('step-job-choice').hidden=true;$('step-job-select').replaceChildren();
    $('step-parent').hidden=true;$('step-dependencies').hidden=true;$('step-dependencies').open=false;
    screen.querySelector('.step-description').open=false;
    $('step-actual-parameters').replaceChildren();
    $('step-group-path').hidden=true;
    activeFile=''; activeSnapshot=null; focusStep=null; info=null;viewerPosition=null; bridge.reset(); $('step-viewer-shell').hidden=true;
    for (const id of ['step-note','step-error','step-progress','step-file-source','step-downloads','step-output','step-code','step-chart','step-figures','step-investigation-log']) $(id).replaceChildren();
    for (const id of ['step-investigation','step-output-section','step-code-section']) $(id).hidden=true;
    for (const section of screen.querySelectorAll('.step-sections > details')) section.open=false;
    $('step-registered-results').open=true;
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
    for (const id of ['step-plan-name','step-title','step-purpose','step-key']) $(id).dataset.noI18n = '';
    $('step-plan-name').textContent=planName || ''; $('step-plan-name').title=planName || '';
    $('step-title').textContent=title;$('step-kind').textContent=t(kind);
    $('step-index').textContent=String(index+1).padStart(2,'0')+' / '+total;
    $('step-key').textContent=id;
    editor('purpose',$('step-purpose'),{prose:true}).update({text:purpose,editable:false,revision:current?.revision});
  }
  function status(label,tone) {
    $('step-status').textContent=label;$('step-status').dataset.state=tone;
  }
  function relations(before,after) {
    const chip=n=>`<button type="button" data-no-i18n data-related="${esc(n.id)}" title="${esc(n.id)}">${esc(taskLabel(n))}</button>`;
    const line=(label,nodes)=>`<div><h3>${t(label)} <small>${nodes.length}</small></h3><div class="relation-chips">${nodes.slice(0,3).map(chip).join('')}${nodes.length>3?`<details><summary>+ ${nodes.length-3}</summary><div class="relation-chips">${nodes.slice(3).map(chip).join('')}</div></details>`:''}</div></div>`;
    $('step-relations').innerHTML=(before.length?line('直接上游',before):'')+(after.length?line('直接下游',after):'');
    $('step-dependencies').hidden=!before.length && !after.length;
    $('step-dependencies').querySelector('summary').textContent=`依赖关系 · 上游 ${before.length} / 下游 ${after.length}`;
  }
  $('step-relations').onclick=event=>{
    const id=event.target.closest('[data-related]')?.dataset.related;
    if(id && current?.related) current.related(id);
  };
  $('step-parent').onclick=()=>current?.related?.($('step-parent').dataset.parent);
  function drawFiles() {
    if(registeredFiles.length) {
      $('step-files').replaceChildren();$('step-structures').hidden=true;
      return;
    }
    if(registeredSelection) {
      $('step-files').innerHTML=`<a href="${esc(registeredSelection.url)}" download="${esc(registeredSelection.name)}" title="下载结构"><svg class="icon" aria-hidden="true"><use href="/assets/ui-icons.svg#i-download"/></svg> ${esc(registeredSelection.name)}</a>`;
      return;
    }
    const options=availableFiles(info);
    $('step-files').innerHTML=options.map(name=>
      `<button type="button" data-step-file="${esc(name)}" aria-pressed="${name===activeFile}">${esc(name)}</button>`).join('') || '<span class="step-empty">'+t('暂无可查看的结构文件')+'</span>';
  }
  function selectFile(name,selection=null) {
    if (!current || !info?.files[name]) return;
    restoreViewer();
    registeredSelection=null;results.clearSelection();
    if(!selection)focusStep=null;
    if(selection && name===activeFile && activeSnapshot===info.ionic_step && frame.hasAttribute('src')) {
      $('step-note').textContent='';if(bridge.seek(selection))return;
    }
    const source = info.files[name], view = current;
    activeFile=name;activeSnapshot=info.ionic_step; drawFiles(); $('step-note').textContent='';
    let caption = source;
    if (name === 'POSCAR' && source === '方案预览' && ['surface','top','bridge','hollow'].includes(view.key)) caption='示意结构（确认后按收敛结果生成）';
    $('step-file-source').textContent=caption;
    bridge.reset();
    bridge.open(name,async () => {
      if (name==='POSCAR' && source==='方案预览' && view.preview) return new Blob([view.preview]);
      const response=await fetch(view.base+'/'+encodeURIComponent(name)+jobQuery(view),{cache:'no-store'});
      if (!response.ok) {
        const error=await response.json().catch(()=>({}));
        throw new Error(error.error?.message || '结构读取失败，请刷新重试。');
      }
      return new File([await response.blob()],response.headers.get('X-Structure-Name')||name);
    },{selection,companions:/\.(dump|lammpstrj)$/i.test(name)?info.structure_companions || []:[]});
  }
  async function refresh(manual=false) {
    const view=current;
    if (!view || (!view.general && view.key==='energies') || view.loading) return;
    view.loading=true;
    const runtime=view.plan?.runtime?.[view.key] || {};
    const initialJob=view.selectedJob || runtime.accepted_jobs?.at(-1) || runtime.jobs?.at(-1) || runtime.generic_job_id;
    const registeredRead=view.genericJob?results.refresh(view.base,initialJob):Promise.resolve(null);
    if(!info?.points?.length)convergence.update({...info,loading:true},view.id+':'+view.key);
    try {
      const next = await api(view.base+'/info'+jobQuery(view));
      if (current!==view) return;
      if(next.editing?.revision && next.editing.revision<view.revision)return;
      const sourceChanged=activeFile && info?.files[activeFile]!==next.files[activeFile];
      info=next;
      let registered=null;
      if(view.genericJob) {
        view.selectedJob=next.selected_job_id || '';
        $('step-job-choice').hidden=!(next.jobs?.length);
        const options=executionOptions(view.plan,view.plan.nodes.find(item=>item.id===view.key),next.jobs);
        $('step-job-select').innerHTML=options.map(job=>`<option value="${esc(job.id)}" ${job.id===view.selectedJob?'selected':''}>${esc(job.label)}</option>`).join('');
        $('step-job-select').title=options.find(job=>job.id===view.selectedJob)?.label || '';
        $('step-job-select').hidden=options.length<2;
        let record=$('step-job-choice').querySelector('.step-current-execution');
        if(!record){record=document.createElement('span');record.className='step-current-execution';$('step-job-choice').append(record);}
        record.hidden=options.length!==1;record.textContent=options[0]?.label || '';
        registered=view.selectedJob===initialJob?await registeredRead:await results.refresh(view.base,view.selectedJob);
        if(current!==view)return;
      }
      if(view.general && view.plan) drawGeneral(view.plan,view.plan.nodes.find(n=>n.id===view.key));
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
      $('step-monitor-label').textContent=t(next.monitor?.kind==='md'?'分子动力学':'离子步收敛');
      $('step-progress').textContent=next.ionic_step!=null
        ? next.monitor?.kind==='md'?`${next.ionic_step} MD 步`:`${next.ionic_step}${view.nsw>0?' / '+view.nsw:''} 步` : '';
      convergence.update({...next,nsw:view.nsw},view.id+':'+view.key+':'+(next.selected_job_id || next.pk || ''));
      if(view.genericJob) $('step-energies').hidden=!!registered;
      $('step-structures').hidden=registered?.kind==='neb' || view.plan?.nodes.find(item=>item.id===view.key)?.kind==='analysis';
      if(registered?.kind==='neb') {
        const latest=registered.snapshots?.at(-1);
        $('step-progress').textContent=latest?`优化步 ${latest.step}`:'';
      }
      if (!registeredFiles.length && !registeredSelection && (!activeFile || sourceChanged)) {
        const nextFile=next.files[activeFile]?activeFile:availableFiles(next)[0];
        if(nextFile && $('step-structures').open && !$('step-structures').hidden) selectFile(nextFile);
        else if(activeFile) {activeFile='';bridge.reset();$('step-viewer-shell').hidden=true;$('step-file-source').textContent='';}
      }
      else if (!registeredFiles.length && !registeredSelection && manual && activeFile!=='POSCAR') selectFile(activeFile);
      if (next.ionic_step!=null) rememberCount(view.id,view.key,next.ionic_step);
    } catch(error) {
      const registered=await registeredRead;
      if(current===view) {
        if(registered)$('step-energies').hidden=true;
        $('step-note').textContent=error.message;
        if(!info?.points?.length)convergence.update({...info,error:true},view.id+':'+view.key);
      }
    } finally { view.loading=false; }
  }
  function jobQuery(view) {return view.selectedJob?'?job_id='+encodeURIComponent(view.selectedJob):'';}
  $('step-job-select').onchange=()=>{
    if(!current) return;
    current={...current,selectedJob:$('step-job-select').value,loading:false};
    results.reset();registeredSelection=null;activeFile='';info=null;bridge.reset();$('step-viewer-shell').hidden=true;
    $('step-progress').textContent='';$('step-note').textContent='';
    convergence.reset();selectedParameter='';$('step-actual-parameters').replaceChildren();$('step-code').replaceChildren();
    for(const key of [...editors.keys()])if(key!=='purpose')editors.delete(key);
    refresh();
  };
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
    const prefix=key==='single'?'':key+'/';
    textDetails({parameters:Object.fromEntries(['INCAR','KPOINTS'].filter(name=>item.files?.[prefix+name]).map(name=>[name,item.files[prefix+name]])),script:item.files?.[prefix+'_aiidasubmit.sh'] || ''});
    const aggregate=key==='energies';
    for (const id of ['step-structures','step-energies','step-refresh','step-progress']) $(id).hidden=aggregate;
    $('step-config').open=false;
    $('step-structures').open=!job; $('step-energies').open=!aggregate && !!job;
    $('step-investigation').hidden=true; $('step-investigation-log').replaceChildren();
    $('step-progress').textContent=''; $('step-note').textContent='';
    $('step-file-source').textContent=''; convergence.reset();
    info={files:current.preview?{POSCAR:'方案预览'}:{},points:[]};
    drawFiles();
    if (!job && !step.reused_pk && current.preview) selectFile('POSCAR');
    screen.parentElement.scrollTop=0;
    $('step-back').focus({preventScroll:true});
    refresh();
  }
  $('step-back').onclick=close;
  $('step-refresh').onclick=()=>{refresh(true);void taskThread.refresh();};
  pendingStep.addEventListener('click', async event => {
    const button = event.target.closest('[data-reconcile-job]');
    const view = current;
    if (!button || !view) return;
    button.disabled = true;
    feedback.hidden = false;
    feedback.textContent = t('正在核对原提交…');
    try {
      const record = await api(`${view.base}/jobs/${encodeURIComponent(button.dataset.reconcileJob)}/reconcile`, {method:'POST'});
      if (current !== view) return;
      feedback.textContent = record.scheduler_job_id
        ? `${t('已关联原作业')} #${record.scheduler_job_id}`
        : t('尚未确认原作业，继续等待调度记录。');
      void refresh();
      void onAttentionChange?.();
    } catch (error) {
      if (current === view) feedback.textContent = error.message;
    } finally { button.disabled = false; }
  });
  $('step-structures').addEventListener('toggle',()=>{
    if(!current || !$('step-structures').open || activeFile || registeredSelection) return;
    if(registeredFiles.length){results.selectImage(registeredFiles.find(item=>item.selected)?.index ?? registeredFiles[0].index,false);return;}
    const name=fileNames.find(name=>info?.files[name]);if(name) selectFile(name);
  });
  $('step-files').onclick=event=>{const image=event.target.closest('[data-result-image]');if(image){results.selectImage(Number(image.dataset.resultImage),false);return;}if(event.target.dataset.stepFile)selectFile(event.target.dataset.stepFile);};
  setInterval(()=>{if(current && (!info?.terminal || current.failed || info?.investigations?.some(row=>!row.terminal)) && !document.hidden && screen.getClientRects().length)refresh();},20000);
  function drawGeneralStatus(plan,node) {
    const items = current.attention || [];
    const issue = items.find(item => item.node_id === node.id);
    const state = plan.runtime[node.id].status;
    if (issue) status(attentionLabel(issue), issue.kind === 'science' ? 'blocked' : state);
    else if (state === 'pending' && items.some(item => item.kind === 'science' && node.depends_on.includes(item.node_id))) {
      status(t('等待上游 NEB 验收'),state);
    } else status(nodeStatus(plan,node),state);
  }
  function drawGeneral(plan,node) {
    const state=plan.runtime[node.id];
    taskThread.update(current.base, plan.task_agents?.[node.id]);
    $('step-parent').hidden=true;
    const path=$('step-group-path');
    path.innerHTML=`<span data-no-i18n>${esc((node.group_path || []).join(' › '))}</span>`;path.hidden=!path.textContent;
    drawGeneralStatus(plan,node);
    const editing={...info?.editing};
    if(!['pending','blocked','ready','waiting'].includes(state.status) || state.generic_job_id || state.job_id || state.jobs?.length)editing.purpose=editing.files=false;
    const parameters=info?.parameters || node.job?.files || node.text_files || {};
    textDetails({purpose:node.purpose,parameters:Object.fromEntries(Object.entries(parameters).filter(([name])=>['INCAR','KPOINTS'].includes(name))),
      script:info?.script ?? (node.code || node.job?.script || node.submission_script || ''),editing,
      codeTitle:['model','analysis'].includes(node.kind)?'Python 代码':'提交脚本'});
    const files=state.outputs?.files || [], result=['analysis','job','task'].includes(node.kind) && state.outputs?.result;
    $('step-output-section').hidden=!result && !files.length;
    $('step-output').textContent=result?JSON.stringify(result,null,2):'';
    const base = current.sessionPath + '/workflows/' + encodeURIComponent(plan.id) + '/artifacts/' + encodeURIComponent(node.id);
    const figures=resultFigures(base,files,node.title);
    if($('step-figures').innerHTML!==figures) $('step-figures').innerHTML=figures;
    $('step-downloads').innerHTML=files.map(name=>`<a download href="${esc(base+'/'+name.split('/').map(encodeURIComponent).join('/'))}">${esc(name)} <span aria-hidden="true">↓</span></a>`).join('');
    $('step-error').textContent=state.error || state.sync_error || '';
    current.plan=plan;
  }
  function updateAttention(planId, items) {
    if (!current?.general || current.id !== planId) return;
    current.attention = items;
    drawGeneralStatus(current.plan,current.plan.nodes.find(node => node.id === current.key));
    const markup = stepAttention(items.filter(item => item.node_id === current.key));
    pendingStep.hidden = !markup;
    if (pendingStep.dataset.content !== markup || (markup && !pendingStep.firstChild)) {
      pendingStep.innerHTML=markup;
      pendingStep.dataset.content=markup;
    }
  }
  function openGeneral(plan,node,sessionPath,items = [],focusItem = null) {
    enter();
    current={id:plan.id,key:node.id,general:true,genericJob:['job','task'].includes(node.kind),revision:plan.revision,title:node.title,sessionPath,
      failed:plan.runtime[node.id].status==='failed',nsw:node.effective?.incar?.NSW,
      related:id=>{const p=current.plan,n=p.nodes.find(n=>n.id===id);if(n) openGeneral(p,n,sessionPath,current.attention);},
      base:`${sessionPath}/workflows/${plan.id}/steps/${encodeURIComponent(node.id)}`};
    if (focusItem?.kind === 'science') current.selectedJob = focusItem.job_id;
    const view=workflowView(plan), siblings=view.children.get(view.owners.get(node.id)) || view.nodes;
    const sequence=view.owners.get(node.id)===node.id?graphLayers(view.nodes).flat():siblings;
    heading(taskLabel(node),kindName(node.kind),node.id,sequence.findIndex(n=>n.id===node.id),sequence.length,plan.title,node.purpose);
    screen.dataset.planId=plan.id;screen.dataset.nodeId=node.id;
    screen.dispatchEvent(new Event('stepchange'));
    const index=new Map(plan.nodes.map(n=>[n.id,n]));
    const visible=view.nodes.find(n=>n.id===node.id);
    relations((visible || node).depends_on.map(id=>index.get(id)).filter(Boolean),(visible?view.nodes:plan.nodes).filter(n=>n.depends_on.includes(node.id)));
    drawGeneral(plan,node);
    $('step-back').focus({preventScroll:true});
    updateAttention(plan.id, items);
    if (focusItem?.kind === 'approval') taskThread.reveal(focusItem.approval_id);
    else if (focusItem) {
      const issue = pendingStep.querySelector(`[data-step-attention-id="${CSS.escape(focusItem.job_id)}"]`);
      issue?.scrollIntoView({block:'nearest'});
      issue?.querySelector('button,summary')?.focus({preventScroll:true});
    }
    $('step-energies').hidden=!['vasp','job','task'].includes(node.kind); $('step-progress').hidden=$('step-energies').hidden;$('step-refresh').hidden=false;
    $('step-structures').hidden=node.kind==='analysis';
    $('step-config').open=false; $('step-structures').open=node.kind==='model';
    const runtime=plan.runtime[node.id];
    $('step-energies').open=node.kind==='vasp' || !!(runtime.generic_job_id || runtime.jobs?.length);
    info={files:{},points:[]}; drawFiles(); refresh();
  }
  return {open,close,openGeneral,updateAttention,updateGeneral(plan) {
    if(!current?.general || current.id!==plan.id) return;
    if(plan.revision<current.revision)return;
    const node=plan.nodes.find(n=>n.id===current.key);
    if(!node) {close();return;}
    current.revision=plan.revision; drawGeneral(plan,node);
  },update(item,job) {
    if (!current || current.id!==item.preview_id) return;
    const step=planSteps(item).find(s=>s.stage===current.key);
    if (!step) return;
    const details=describeStep(item,job,step);
    status(details.status,details.tone);
  }};
}
