import test from 'node:test';
import assert from 'node:assert/strict';
import {activity, activitySteps, activityDetail} from '../src/chemistry_agent/web/answer-ui.mjs';

test('collapsed activity does not serialize or embed large scientific outputs', () => {
  const calls = [{call_id:'large',name:'build_structure', success:true,
    arguments:{code:'x'.repeat(500000)}, result:{toJSON(){throw new Error('eager JSON render');}}}];
  const folded = activity({id:'test',calls,deferred:true});
  assert.ok(folded.length < 1000);
  assert.match(folded,/data-deferred-activity/);
  assert.doesNotMatch(folded, /<pre>/);
  const names = activitySteps([], calls, {deferDetails:true});
  assert.match(names,/ASE 代码建模/);
  assert.match(names,/data-deferred-call/);
  assert.ok(names.length < 500);
});
test('expanded tool parameters remain complete and escaped', () => {
  const code = '<script>'.repeat(10000);
  const calls = [{call_id:'one',arguments:{code},result:{energy:-10}}];
  const detail = activityDetail([{type:'tool',call_id:'one'}],calls,0);
  assert.equal((detail.match(/&lt;script&gt;/g)||[]).length,10000);
  assert.ok(detail.includes("&quot;energy&quot;: -10"));
  assert.doesNotMatch(detail,/<script>/);
});
