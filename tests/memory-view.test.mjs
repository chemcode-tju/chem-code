import assert from 'node:assert/strict';
import test from 'node:test';
import {createRequire} from 'node:module';
import {createMarkdownRenderer} from '../src/chemistry_agent/web/core.mjs';
import {documentBody, resultCard, scopeDescription} from '../src/chemistry_agent/web/memory-view.mjs';

const require=createRequire(import.meta.url);
const markdown=createMarkdownRenderer(require('../src/chemistry_agent/web/vendor/markdown-it.min.js'));
const item={title:'BRMIX', kind:'lesson', version:1, session_id:'source', updated_at:'2026-11-11T00:00:00Z',
  evidence:{level:'converged', electronic_converged:true, ionic_converged:null, scientifically_validated:false},
  parameters:{incar:{AMIX:0.02}}, content:JSON.stringify({original_error:'XML overflow', proposed_reason:'Mixing hypothesis',
    retry_outcome:'completed', parameter_changes:[{parameter:'incar.AMIX',reference:0.1,current:0.02,reference_present:true,current_present:true},
      {parameter:'incar.IMIX',reference:null,current:1,reference_present:false,current_present:true}]})};

test('repair details retain direction, unknown values, and evidence limits',()=>{
  const html=documentBody(item,'Cu111 source',markdown);
  assert.match(html,/修复前 → 修复后/);
  assert.match(html,/<td data-no-i18n>incar.AMIX<\/td><td data-no-i18n>0.1<\/td><td data-no-i18n>0.02<\/td>/);
  assert.match(html,/<td data-no-i18n>incar.IMIX<\/td><td>未显式记录<\/td><td data-no-i18n>1<\/td>/);
  assert.match(html,/<td>电子收敛<\/td><td>通过<\/td>/);
  assert.match(html,/<td>离子收敛<\/td><td>未记录<\/td>/);
  assert.match(html,/<td>科学精度<\/td><td>未验证<\/td>/);
  assert.match(html,/完整原始记录与代码/);
});

test('untrusted memory fields, source names and Markdown never become active HTML',()=>{
  const hostile='<img src=x onerror=alert(1)><script>bad()</script>';
  const record={...item, title:hostile, session_title:hostile, applicability:hostile, systems:[hostile],
    content:JSON.stringify({original_error:hostile,proposed_reason:hostile,parameter_changes:[{parameter:hostile, current:hostile}]})};
  for (const html of [documentBody(record,hostile,markdown), resultCard(record),
    documentBody({...record,kind:'document',content:hostile+'\n[link](javascript:alert(1))'},hostile,markdown)]) {
    assert.doesNotMatch(html,/<img|<script|href="javascript:/);
    assert.match(html,/&lt;img/);
  }
});

test('legacy lesson prose renders as Markdown rather than failing JSON parsing',()=>{
  assert.match(documentBody({...item,content:'**检查 SCF**'},'source',markdown),/<strong>检查 SCF<\/strong>/);
  assert.match(scopeDescription('project'),/同一项目内切换对话，结果可能相同/);
  assert.match(scopeDescription('session'),/仅检索所选对话/);
});
