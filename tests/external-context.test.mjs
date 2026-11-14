import test from 'node:test';
import assert from 'node:assert/strict';
import { externalContextView as view } from '../src/chemistry_agent/web/external-context.mjs';

test('missing telemetry has no placeholders or empty meter', () => {
  assert.deepEqual(view({managed_by:'antigravity'}).rows, []);
  assert.equal(view({managed_by:'antigravity'}).visible, false);
  assert.equal(view({managed_by:'codex', manual_supported:true}).visible, true);
});
test('native zero remains a valid measurement, absent capacity hides percentage', () => {
  assert.equal(view({used_tokens:0, capacity_tokens:1000}).percent, 0);
  assert.equal(view({used_tokens:1234}).percent, null);
  assert.equal(view({used_tokens:1234, capacity_tokens:10000}).percent, 12.3);
});
test('only actual compaction evidence is displayed', () => {
  assert.equal(view({compaction:{status:'unknown'}}).rows.length, 0);
  const result = view({manual_supported:true, compaction:{status:'running'}});
  assert.equal(result.disabled, true);
  assert.equal(result.manual, true);
  assert.equal(result.rows[0][1], '正在压缩');
  assert.equal(view({compaction:{status:'completed', before_tokens:1000}}).rows.length, 1);
});
