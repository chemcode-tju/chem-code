import assert from 'node:assert/strict';
import test from 'node:test';
import {buildTurns} from '../src/chemistry_agent/web/core.mjs';
import {activity, activityOpen} from '../src/chemistry_agent/web/answer-ui.mjs';
import {liveAnswer} from '../src/chemistry_agent/web/live-answer.mjs';

test('all public intermediate messages survive final rendering in tool order',()=>{
  const messages=[{role:'user',content:'查参数'},
    {role:'assistant',content:'先查已有记录。',reasoning_content:'private',tool_calls:[{id:'a',function:{name:'memory_search',arguments:'{}'}}]},
    {role:'tool',tool_call_id:'a',content:'{"items":[]}'},
    {role:'assistant',content:'然后比较参数。',tool_calls:[{id:'b',function:{name:'memory_compare',arguments:'{}'}}]},
    {role:'tool',tool_call_id:'b',content:'{"differences":[]}'},
    {role:'assistant',content:'最终结果。'}];
  const [turn]=buildTurns(messages);
  assert.equal(turn.answer,'最终结果。');
  assert.deepEqual(turn.steps.map(row=>row.text || row.name),['先查已有记录。','memory_search','然后比较参数。','memory_compare']);
  const html=activity({id:'a',steps:turn.steps,calls:turn.calls});
  assert.match(html,/先查已有记录/); assert.match(html,/然后比较参数/);
  assert.doesNotMatch(html,/private|最终结果| open[ >]/);
  assert.match(activity({id:'b',steps:turn.steps,running:true,draftRequest:'request'}),/data-running="true" open/);
  assert.doesNotMatch(activity({id:'c',steps:[{type:'commentary',text:'<img src=x onerror=bad()>'}]}),/<img/);
});

test('polling preserves user expansion choices and completion folds the live process once',()=>{
  assert.equal(activityOpen(true),true);
  assert.equal(activityOpen(true,{running:true,open:false}),false);
  assert.equal(activityOpen(false,{running:true,open:true}),false);
  assert.equal(activityOpen(false,{running:false,open:true}),true);
});

test('stream resets only the draft; steps survive polling and reconnect without duplication',()=>{
  const channels=[], frames=[];
  let settled=0;
  const original=globalThis.EventSource;
  globalThis.EventSource=class {
    listeners={}; closed=false;
    constructor(url) { this.url=url; channels.push(this); }
    addEventListener(name,handler) { this.listeners[name]=handler; }
    close() { this.closed=true; }
    send(name,body) { this.listeners[name]({data:JSON.stringify(body)}); }
  };
  try {
    const stream=liveAnswer({render:(request,text,steps)=>frames.push({request,text,steps}),settle:()=>settled++});
    stream.follow('session','request'); const channel=channels[0]; channel.onopen();
    channel.send('draft',{append:true,text:'我现在要进行下一步',steps:[]});
    const steps=[{type:'commentary',text:'我现在要进行下一步'},{type:'tool',name:'memory_search',status:'running'}];
    channel.send('draft',{append:false,text:'',steps});
    assert.deepEqual(frames.at(-1).steps,steps);
    channel.send('draft',{append:true,text:'正在比较'});
    stream.follow('session','request');
    assert.equal(channels.length,1); assert.equal(frames.at(-1).text,'正在比较');
    assert.deepEqual(frames.at(-1).steps,steps);
    channel.onopen(); channel.send('draft',{append:true,text:'正在比较',steps});
    assert.equal(frames.at(-1).text,'正在比较'); assert.equal(frames.at(-1).steps.length,2);
    stream.follow('other','new');
    const count=frames.length; channel.send('draft',{text:'stale',steps:[]});
    assert.equal(frames.length,count); assert.equal(channel.closed,true);
    channels[1].send('settled',{}); assert.equal(settled,1);
  } finally { globalThis.EventSource=original; }
});
