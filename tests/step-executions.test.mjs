import test from 'node:test';
import assert from 'node:assert/strict';
import {executionOptions} from '../src/chemistry_agent/web/step-executions.mjs';

test('one execution list preserves failed attempts, adopted records and scheduler facts',()=>{
  const task={id:'neb',title:'过渡态搜索',kind:'task'};
  const plan={nodes:[task,{id:'attempt',title:'初始路径',kind:'job'},{id:'retry',title:'缩小步长重试',kind:'job'}],runtime:{
    neb:{jobs:['first','second'],accepted_jobs:['second']},
    attempt:{generic_job_id:'first'},retry:{generic_job_id:'second'},
  }};
  const original=structuredClone(plan);
  const options=executionOptions(plan,task,[{id:'first',scheduler_job_id:'201',state:'FAILED'},{id:'second',scheduler_job_id:'202',state:'completed'}]);
  assert.deepEqual(options,[{id:'first',label:'#201 · 失败 · 初始路径'},{id:'second',label:'#202 · 调度完成 · 已采用 · 缩小步长重试'}]);
  assert.doesNotMatch(options[1].label,/收敛/);
  assert.deepEqual(plan,original);
});

test('unmapped records and unknown states stay visible in the execution selector',()=>{
  assert.deepEqual(executionOptions({nodes:[],runtime:{}},{id:'step'},[{id:'job-uuid',state:'special_scheduler_state'}]),[{id:'job-uuid',label:'#job-uuid · special_scheduler_state'}]);
});
