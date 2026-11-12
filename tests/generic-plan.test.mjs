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
