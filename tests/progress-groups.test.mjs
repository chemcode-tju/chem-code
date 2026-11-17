import test from 'node:test';
import assert from 'node:assert/strict';
import {latestStepProgress,currentStepProgress,createProgressFeed,progressStatus} from '../src/chemistry_agent/web/progress-feed.mjs';

test('a successful retry replaces the obsolete failure in the current step groups',()=>{
  const failed={seq:2,kind:'step',plan_id:'a',node_id:'neb',status:'failed'};
  const completed={...failed,seq:4,status:'completed'};
  const commentary={...failed,seq:5,kind:'interpretation',status:'failed'};
  const otherPlan={...failed,seq:3,plan_id:'b',status:'running'};
  assert.deepEqual(latestStepProgress([completed,failed,commentary,otherPlan]),[completed,otherPlan]);
});

test('unknown states are neutral and completion does not imply scientific convergence',()=>{
  assert.deepEqual(progressStatus('completed'),{group:'complete',label:'已完成'});
  assert.deepEqual(progressStatus('unconverged'),{group:'attention',label:'未收敛'});
  assert.deepEqual(progressStatus('pending'),{group:'active',label:'等待依赖'});
  assert.deepEqual(progressStatus('new_status'),{group:'other',label:'new_status'});
  assert.deepEqual(latestStepProgress([{seq:1,kind:'plan',plan_id:'a',node_id:null,status:'completed'}]),[]);
});

const event=(node,status,seq=1,kind='step')=>({seq,kind,plan_id:'plan',node_id:node,status,message:node,created_at:'2026-11-15T01:00:00Z'});
const plan=(runtime)=>({id:'plan',updated_at:'2026-11-15T02:00:00Z',nodes:[
  {id:'relax',kind:'task',title:'初态优化',depends_on:[]},
  {id:'frequency',kind:'analysis',title:'振动分析',depends_on:['relax']},
  {id:'attempt',kind:'job',parent_task:'relax',title:'失败的计算',depends_on:[]},
],runtime});

test('repair pending replaces the failed event while runtime is unavailable',()=>{
  const failed=event('relax','failed',1),repair=event('relax','pending',2,'repair');
  assert.deepEqual(currentStepProgress([failed,repair]),[repair]);
});

test('runtime reopens repaired and blocked steps even before a new step event exists',()=>{
  const rows=[event('relax','failed'),event('frequency','blocked',2)];
  const current=currentStepProgress(rows,[plan({relax:{status:'pending'},frequency:{status:'pending'},attempt:{status:'failed'}})]);
  assert.deepEqual(current.map(row=>[row.node_id,row.status]),[['relax','pending'],['frequency','pending']]);
  assert(current.every(row=>row.created_at==='2026-11-15T02:00:00Z'));
  assert.equal(rows[0].status,'failed','Historical events are retained unchanged');
});

test('completed research step owns its failed child, which remains only in event history',()=>{
  const rows=[event('attempt','failed',8),event('relax','completed',7)];
  const runtime={relax:{status:'completed',completed_at:'2026-11-15T01:30:00Z'},frequency:{status:'running'},attempt:{status:'failed'}};
  const current=currentStepProgress(rows,[plan(runtime)]);
  assert.deepEqual(current.map(row=>[row.node_id,row.status]),[['relax','completed'],['frequency','running']]);
  assert.equal(current[0].created_at,runtime.relax.completed_at);
  assert.equal(current.filter(row=>progressStatus(row.status).group==='attention').length,0);
  assert.equal(rows.find(row=>row.node_id==='attempt').status,'failed');
});

test('running runtime preserves a real scheduler queue and does not infer convergence',()=>{
  const source={id:'plan',nodes:[{id:'job',kind:'job',depends_on:[],title:'计算'}],runtime:{job:{status:'running',job:{scheduler_state:'queued',scheduler_job_id:'204172'}}}};
  const current=currentStepProgress([],[source]);
  assert.equal(current[0].status,'queued');assert.equal(current[0].detail.scheduler_job_id,'204172');
  source.runtime.job.status='completed';
  assert.equal(currentStepProgress([],[source])[0].status,'completed');
});

test('switching sessions rejects stale plans and event responses while retaining historical failure',async()=>{
  const originalDocument=globalThis.document;
  const elements=new Map();
  globalThis.document={documentElement:{lang:'zh-CN'},getElementById(id){
    if(!elements.has(id))elements.set(id,{innerHTML:'',dataset:{},addEventListener(){}});
    return elements.get(id);
  }};
  try {
    let session='a',completeA;
    const feed=createProgressFeed({getSession:()=>session,markdown:{render:text=>text},onFinal:async()=>false,
      api:url=>url.includes('/a/')?new Promise(resolve=>{completeA=resolve;}):Promise.resolve({events:[event('attempt','failed')],next_cursor:1})});
    feed.setPlans('a',[plan({relax:{status:'failed'}})]);
    const pending=feed.refresh('a');
    session='b';feed.reset('b');
    feed.setPlans('b',[plan({relax:{status:'completed'},frequency:{status:'pending'},attempt:{status:'failed'}})]);
    await feed.refresh('b');
    const html=elements.get('progress-events').innerHTML;
    assert.doesNotMatch(html,/data-progress-group="attention"/);
    assert.match(html,/data-progress-group="history"[^]*失败/,'Failed child stays visible in historical updates');
    feed.setPlans('a',[plan({relax:{status:'failed'}})]);
    await feed.refresh('a');
    completeA({events:[event('relax','failed')],next_cursor:1});await pending;
    assert.equal(elements.get('progress-events').innerHTML,html,'Late responses must not replace the current session');
    session='c';feed.reset('c');
    assert.doesNotMatch(elements.get('progress-events').innerHTML,/初态优化|失败的计算/);
  } finally {globalThis.document=originalDocument;}
});
