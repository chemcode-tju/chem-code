import assert from 'node:assert/strict';
import test from 'node:test';
import {activity, activitySteps, answerBox, answerFooter, liveAnswerFooter, progressMarkdown, visibleReference} from '../src/chemistry_agent/web/answer-ui.mjs';

test('only generated revision envelopes are omitted from chat presentation', () => {
  const generated = '修改方案/步骤：Cu(100) 优化；先读取 calculation_plan_get，再用 calculation_plan_save 修改 plan_id=abc-123，expected_revision=3。';
  assert.equal(visibleReference(generated), '');
  assert.equal(visibleReference('修改步骤：表面；用 calculation_prepare.revises_preview_id 替换草案 abc-123'), '');
  for (const source of ['论文摘要：Cu(100)', '说明 calculation_plan_get 的作用', '修改方案/步骤：我的原始笔记', generated+'\n附：实验条件']) assert.equal(visibleReference(source), source);
});

test('completed plain answers have a duration, not an empty expandable log', () => {
  const html = activity({id:'plain',start:'2026-11-17T00:00:00Z',end:'2026-11-17T00:00:04Z'});
  assert.match(html,/4 秒/);
  assert.doesNotMatch(html,/<details|未调用工具/);
  assert.match(activity({id:'live',start:'2026-11-17T00:00:00Z',running:true}),/<details/);
  assert.match(activity({id:'comment',steps:[{type:'commentary',text:'Checking the result'}]}),/Checking the result/);
});

test('tool failure does not assume incorrect user input', () => {
  const html=activitySteps([], [{name:'remote_shell',success:false,arguments:{command:'pwd'},result:{error:'timeout'}}]);
  assert.match(html,/未完成/); assert.match(html,/timeout/); assert.doesNotMatch(html,/需调整输入/);
});

test('message footer can follow attachments while preserving completed and streaming copy sources', () => {
  const completed=answerBox('<p>Energy = -12.35 eV</p>');
  assert.doesNotMatch(completed,/data-copy/);
  const withFiles=completed+'<div data-generated-run="run-1">result.xyz</div>'+answerFooter(3);
  assert.ok(withFiles.indexOf('result.xyz')<withFiles.indexOf('data-copy="3"'));
  const progress=progressMarkdown({render:source=>`<p>${source}</p>`},'Energy = -12.35 eV');
  assert.match(progress,/data-source="Energy = -12.35 eV"/);
  assert.ok(progress.indexOf('</p>')<progress.indexOf('data-copy-markdown'));
  const live=activity({id:'live',running:true,draftRequest:'request-1'});
  assert.doesNotMatch(live,/data-copy-live/);
  assert.match(liveAnswerFooter('request-1'),/data-copy-live="request-1"/);
  assert.match(liveAnswerFooter('request-1'),/data-live-footer="request-1" hidden/);
});
