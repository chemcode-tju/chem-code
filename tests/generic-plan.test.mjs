import assert from 'node:assert/strict';
import test from 'node:test';
import {generalRows, nodeStatus, renderGeneralPlan} from '../src/chemistry_agent/web/general-plan.mjs';
import {kindName} from '../src/chemistry_agent/web/plan-layout.mjs';

test('generic jobs are primary calculation cards with script resources and file dependencies', () => {
  const node = {id:'md', title:'LAMMPS', kind:'job', inputs:{}, depends_on:[],
    files:[{node:'prepare',path:'atoms.data',target:'data/atoms.data'}],
    job:{host:'cluster-b',script:'srun lmp -in input.lmp',resources:{queue:'low',wallclock_seconds:60},retrieve:['log.lammps']}};
  const plan = {id:'plan',title:'MD',revision:1,status:'running',changed_nodes:[],history:[],nodes:[node],
    runtime:{md:{status:'running',job:{backend:'slurm',status:'quota_wait',paused:true}}}};
  assert.equal(kindName('job'),'Slurm 任务');
  assert.equal(nodeStatus(plan,node),'等待队列名额');
  const html = renderGeneralPlan(plan);
  assert.ok(html.includes('0/1 项计算'));
  assert.ok(!/<div class="map-position"[^>]*\bdata-aux(?:\s|>)/.test(html));
  const rows = generalRows(plan,node);
  assert.ok(rows.includes('low') && rows.includes('log.lammps') && rows.includes('atoms.data'));
});

test('adopted job displays observed metadata without inventing submission resources', () => {
  const node={id:'external',kind:'job',job:{adopted:true,host:'lab',resources:null,retrieve:[]}};
  const plan={nodes:[node],runtime:{external:{job:{queue:'debug',remote_directory:'/lab/orca'}}}};
  const rows=generalRows(plan,node);
  assert.ok(rows.includes('接管已有作业') && rows.includes('/lab/orca') && rows.includes('debug'));
  assert.ok(!rows.includes('运行时限') && !rows.includes('undefined'));
});

test('research completion is separate from a retained failed attempt', () => {
  const task={id:'search',title:'选择位点',kind:'task',depends_on:[],inputs:{}};
  const attempt={id:'failed',title:'原失败尝试',kind:'job',parent_task:'search',depends_on:[]};
  const plan={id:'p',title:'研究方案',status:'completed',revision:3,history:[],changed_nodes:[],nodes:[task,attempt],
    runtime:{search:{status:'completed',jobs:['j']},failed:{status:'failed',generic_job_id:'j',job:{scheduler_job_id:'42'}}}};
  assert.ok(renderGeneralPlan(plan).includes('1/1 项计算'));
  assert.ok(generalRows(plan,task).includes('42'));
  assert.equal(kindName('task'),'研究步骤');
});

test('canvas groups explicitly owned jobs and projects their external dependencies without mutating the plan', async()=>{
  const {workflowView,childJobSummary}=await import('../src/chemistry_agent/web/plan-presentation.mjs');
  const nodes=[{id:'prepare',kind:'model',depends_on:[]},
    {id:'relax',kind:'task',depends_on:['prepare']},
    {id:'a',kind:'job',title:'attempt <1>',parent_task:'relax',depends_on:['prepare']},
    {id:'b',kind:'job',parent_task:'relax',depends_on:['a']},
    {id:'freq',kind:'task',depends_on:['b']},
    {id:'standalone',kind:'job',parent_task:'missing',depends_on:[]}];
  const plan={id:'test',title:'Test',revision:1,status:'running',changed_nodes:[],history:[],nodes,
    runtime:Object.fromEntries(nodes.map(n=>[n.id,{status:'pending'}]))};
  plan.runtime.relax={status:'completed'};
  plan.runtime.a={status:'failed',job:{scheduler_job_id:'204172'}};
  plan.runtime.b={status:'running',job:{scheduler_state:'pending'}};
  const before=JSON.stringify(plan),view=workflowView(plan);
  assert.deepEqual(view.nodes.map(n=>n.id),['prepare','relax','freq','standalone']);
  assert.deepEqual(view.nodes.find(n=>n.id==='freq').depends_on,['relax']);
  assert.deepEqual(view.children.get('relax').map(n=>n.id),['a','b']);
  assert.match(childJobSummary(plan,view.children.get('relax')),/2 项作业 · 1 失败 · 1 排队/);
  const html=renderGeneralPlan(plan);
  assert.doesNotMatch(html,/data-vertex="[ab]"/);
  assert.match(html,/data-vertex="relax"/);assert.match(html,/204172/);
  assert.doesNotMatch(html,/<1>/);
  assert.equal(plan.runtime.relax.status,'completed');
  assert.equal(JSON.stringify(plan),before);
});

test('interleaved ownership never introduces a dependency cycle into the canvas',async()=>{
  const {workflowView}=await import('../src/chemistry_agent/web/plan-presentation.mjs');
  const {graphLayers}=await import('../src/chemistry_agent/web/general-plan.mjs');
  const nodes=[{id:'a',kind:'task',depends_on:[]},{id:'b',kind:'task',depends_on:['a']},
    {id:'job',kind:'job',parent_task:'a',depends_on:['b']}];
  const view=workflowView({nodes,runtime:{}});
  assert.equal(graphLayers(view.nodes).flat().length,3);
});
