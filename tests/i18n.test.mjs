import assert from 'node:assert/strict';
import test from 'node:test';
import {setLanguage, t} from '../src/chemistry_agent/web/i18n.mjs';

test('plan runtime labels translate without changing progress values or units', () => {
  setLanguage('en');
  assert.equal(t('执行中 · 6/9 项计算 · 就绪即提交'), 'Running · 6/9 calculations · Submit when ready');
  assert.equal(t('依赖关系 · 上游 3 / 下游 2'), 'Dependencies · 3 upstream / 2 downstream');
  assert.equal(t('v29 · 修改 1 步'), 'v29 · 1 step changed');
  assert.equal(t('10 / 12 步'), '10 / 12 steps');
  assert.equal(t('Cu(100) · -1.25 eV · 298.15 K'), 'Cu(100) · -1.25 eV · 298.15 K');
  assert.equal(t('已启用 3 个节点'), 'Enabled nodes: 3');
});

test('notification and tool-count patterns retain all counts', () => {
  setLanguage('en');
  assert.equal(t('通知 · 3 条未读'), 'Notifications · 3 unread');
  assert.equal(t('会话记忆 · 已摘要 12 / 30 条消息'), 'Chat memory · 12 of 30 messages summarized');
  assert.equal(t('已使用 8 次研究工具 · 2 次需调整输入'), 'Used 8 research tools · 2 need attention');
  assert.equal(t('展开全部 24 个文件'), 'Show all 24 files');
});

test('shared controls and new workspaces use the selected locale', () => {
  setLanguage('en');
  for (const [source, output] of [['看板','Board'], ['已完成','Completed'], ['等待依赖','Waiting for dependencies'], ['程序路径','Program path'], ['完全访问','Full access'], ['科研记忆','Research memory']]) {
    assert.equal(t(source), output);
  }
  setLanguage('zh-CN');
  assert.equal(t('执行中 · 6/9 项计算 · 就绪即提交'), '执行中 · 6/9 项计算 · 就绪即提交');
  assert.equal(t('科研记忆'), '科研记忆');
});
