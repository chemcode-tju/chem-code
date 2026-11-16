import assert from 'node:assert/strict';
import test from 'node:test';
import {kanbanState,planKanban} from '../src/chemistry_agent/web/plan-kanban.mjs';
import {renderGeneralPlan} from '../src/chemistry_agent/web/general-plan.mjs';

test('board separates waiting, running, failures, completion and stopped steps',()=>{
  for(const state of ['pending','draft','idle']) assert.equal(kanbanState(state),'todo');
  for(const state of ['running','preparing','submitted','queued']) assert.equal(kanbanState(state),'active');
  for(const state of ['failed','blocked','paused']) assert.equal(kanbanState(state),'attention');
  for(const state of ['completed','done']) assert.equal(kanbanState(state),'done');
  for(const state of ['cancelled','stopped']) assert.equal(kanbanState(state),'ended');
  assert.equal(kanbanState('future_state'),'other');
  assert.equal(kanbanState(undefined),'other');
});

test('board projects research runtime once, retaining paused jobs and ignoring historical failed children',()=>{
  const nodes=[{id:'done',title:'优化',kind:'task',depends_on:[]},
    {id:'old',title:'失败尝试',kind:'job',parent_task:'done',depends_on:[]},
    {id:'paused',title:'振动',kind:'job',depends_on:['done']}];
  const plan={id:'p',title:'研究',revision:1,status:'running',nodes,history:[],changed_nodes:[],runtime:{
    done:{status:'completed'},old:{status:'failed'},paused:{status:'running',job:{paused:true}},
  }};
  const before=JSON.stringify(plan),html=renderGeneralPlan(plan);
  assert.equal((html.match(/data-board-node="done"/g)||[]).length,1);
  assert.doesNotMatch(html,/data-board-node="old"/);
  assert.match(html,/data-column="attention"[\s\S]*?data-board-node="paused"/);
  assert.match(html,/data-column="done"[\s\S]*?data-board-node="done"/);
  assert.equal(JSON.stringify(plan),before);
});

test('board keeps exact labels and detail action, escapes titles, and represents empty columns',()=>{
  const html=planKanban([{id:'x"',title:'<long title>',state:'pending',status:'等待依赖',groupPath:['分支 A']}],'data-general-step');
  assert.match(html,/data-general-step="x&quot;"/);
  assert.match(html,/&lt;long title&gt;/);
  assert.match(html,/等待依赖/);
  assert.match(html,/data-column="ended"[^>]*hidden/);
  assert.match(html,/<p class="kanban-empty" >暂无步骤<\/p>/);
  assert.equal((html.match(/data-board-node=/g)||[]).length,1);
});
