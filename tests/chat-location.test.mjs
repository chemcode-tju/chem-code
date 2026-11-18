import test from 'node:test';
import assert from 'node:assert/strict';
import {folderLabel,selectHost} from '../src/chemistry_agent/web/chat-location.mjs';

test('changing server resets its folder but preserves independently selected monitoring',()=>{
  const current={host_id:'local',backend:'codex',instance_id:'local-codex',directory:'/Users/me/work',monitor_host:'cluster-b',permission:'user'};
  const agents=[{host_id:'cluster-a',id:'cluster-a-codex',backend:'codex',model:'cpu-model',enabled:true}];
  const selected=selectHost(current,'cluster-a',agents);
  assert.equal(selected.directory,'');assert.equal(selected.monitor_host,'cluster-b');
  assert.equal(selected.instance_id,'cluster-a-codex');assert.equal(selected.model,'cpu-model');
  assert.equal(selectHost(selected,'cluster-a',agents),selected);
  assert.equal(selectHost(current,'not-configured',agents),null);
});

test('optional folders have short labels without modifying the stored path',()=>{
  assert.equal(folderLabel(''),'文件夹');assert.equal(folderLabel('/'),'/');
  assert.equal(folderLabel('/data/home/me/Cu100/'),'Cu100');
});
