import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";
import { buildTurns, contextBadge, createMarkdownRenderer, escapeHtml, metricValue, splitReference } from "../src/chemistry_agent/web/core.mjs";

const require = createRequire(import.meta.url);
const markdown = createMarkdownRenderer(require("../src/chemistry_agent/web/vendor/markdown-it.min.js"));

test("untrusted titles and user text are escaped", () => {
  assert.equal(escapeHtml('<img src=x onerror="alert(1)">'), "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;");
  assert.equal(escapeHtml(null), "");
});

test("Markdown handles formatting and blocks active HTML, unsafe links, and remote images", () => {
  assert.match(markdown.render("**乙醇**"), /<strong>乙醇<\/strong>/);
  assert.doesNotMatch(markdown.render('<script>alert(1)</script><img src=x onerror="bad()">'), /<script|<img/);
  assert.doesNotMatch(markdown.render("[bad](javascript:alert(1))"), /href="javascript/);
  assert.doesNotMatch(markdown.render("![figure](https://example.com/pixel.png)"), /<img/);
  assert.match(markdown.render("[文献](https://example.com)"), /rel="noopener noreferrer"/);
});

test("saved reference excerpts are kept separate from the user's question", () => {
  assert.deepEqual(splitReference("问题\n\n<user_provided_reference>\n原文\n</user_provided_reference>"), { text: "问题", context: "原文" });
  assert.deepEqual(splitReference("<user_provided_reference>只是标签"), { text: "<user_provided_reference>只是标签", context: "" });
});

test("wire conversations reconstruct tool exchanges and exclude reasoning", () => {
  const messages = [
    { role: "user", content: "计算 CCO" },
    { role: "assistant", reasoning_content: "private", tool_calls: [
      { id: "call-1", function: { name: "molecular_properties", arguments: '{"smiles":"CCO"}' } },
    ] },
    { role: "tool", tool_call_id: "call-1", content: '{"formula":"C2H6O"}' },
    { role: "assistant", content: "计算结果是乙醇。", reasoning_content: "private" },
    { role: "user", content: "继续" },
    { role: "assistant", content: "好的。" },
  ];
  const runs = [
    { status: "completed", started_at: "2026-10-31T02:00:00Z", usage: { total_tokens: 20 } },
    { status: "failed", started_at: "2026-10-31T01:00:00Z" },
    { status: "completed", started_at: "2026-10-31T00:00:00Z", usage: { total_tokens: 10 } },
  ];
  const turns = buildTurns(messages, runs);
  assert.equal(turns.length, 2);
  assert.equal(turns[0].calls[0].name, "molecular_properties");
  assert.deepEqual(turns[0].calls[0].arguments, { smiles: "CCO" });
  assert.equal(turns[0].calls[0].result.formula, "C2H6O");
  assert.equal(turns[0].run.usage.total_tokens, 10);
  assert.doesNotMatch(JSON.stringify(turns), /private/);
  assert.equal(buildTurns(messages, runs.slice(0, 1))[0].run, null);
});

test("bad tool payloads are shown as failures and numerical output stays finite", () => {
  const turns = buildTurns([
    { role: "user", content: "计算" },
    { role: "tool", tool_call_id: "unknown", content: "not json" },
  ]);
  assert.equal(turns[0].calls[0].success, false);
  assert.equal(metricValue(NaN), "—");
  assert.equal(metricValue(0.285714, 6), "0.285714");
  assert.equal(metricValue(46.069), "46.069");
});

test("compression badges distinguish summaries and tool excerpts, including legacy runs", () => {
  assert.equal(contextBadge(undefined), null);
  assert.equal(contextBadge({ compressed: false }), null);
  const summary = contextBadge({ compressed: true, summary_used: true, input_tokens_after: 3000,
    input_budget_tokens: 8000, summarized_messages: 12, retained_turns: 4 });
  assert.equal(summary.label, "上下文已压缩");
  assert.match(summary.description, /12 条历史消息/);
  assert.match(summary.description, /原始记录完整保存/);
  assert.equal(contextBadge({ compressed: true, summary_used: false }).label, "工具结果已精简");
});

test("manual compaction does not shift usage onto a conversation turn", () => {
  const turns = buildTurns([
    { role: "user", content: "问" }, { role: "assistant", content: "答" },
  ], [
    { id: "chat", status: "completed", started_at: "2026-01-01", context: {} },
    { id: "compact", status: "completed", started_at: "2026-01-02", context: { manual_compaction: true } },
  ]);
  assert.equal(turns[0].run.id, "chat");
});

test('task cards show completion and recovery while escaping diagnostic text', async () => {
  const { renderTaskStatus } = await import('../src/chemistry_agent/web/calculations.mjs');
  const html = renderTaskStatus({
    pk: 52, finished_ok: true, scheduler_job_id: '202442',
    display_title: '计算完成，结果已取回',
    results: { total_energies: { energy_extrapolated: -14.23149943 },
      run_status: { electronic_converged: true, ionic_converged: true, nsw: 100 } },
  }, {state: 'needs_user', result: {reason: '<script>bad()</script>'}});
  assert.match(html, /计算完成/);
  assert.match(html, /-14.23149943 eV/);
  assert.match(html, /电子收敛：通过/);
  assert.match(html, /结构收敛：通过/);
  assert.match(html, /需要你处理/);
  assert.ok(!html.includes('<script>'));
});

test('task input and status are folded; only the finished output can be downloaded', async () => {
  const {renderTaskCard} = await import('../src/chemistry_agent/web/calculations.mjs');
  const preview = {preview_id:'internal-id', input_sha256:'private-checksum', description:'尚未做精度收敛', structure:{formula:'Cu16'}, request:{incar:{ENCUT:520},kmesh:[6,6,1]}, files:{POSCAR:'test coordinates',INCAR:'ENCUT = 520'}};
  const job = {id:'output-id',pk:70,finished_ok:true,output_structure:{},process_state:'finished'};
  const html = renderTaskCard(preview,job,null,'/session');
  assert.match(html,/ENCUT 520 eV · k 6×6×1/);
  assert.match(html,/data-fold="status"><summary><strong>计算完成/);
  assert.match(html,/data-fold="inputs"/); assert.match(html,/data-view-input/);
  assert.match(html,/\/session\/jobs\/output-id\/structure.extxyz/);
  assert.doesNotMatch(html, /private-checksum|尚未做精度收敛|科学精度|<details[^>]+ open/);
  assert.doesNotMatch(renderTaskCard(preview,{...job,finished_ok:false},null,'/session'),/下载优化结构/);
});

test('history separates attached structure links from user text and references', () => {
  const structure = {id:'00000000-0000-4000-8000-000000000001',name:'Cu slab'};
  const text = '查看\n\n<user_provided_reference>\nnotes\n</user_provided_reference>\n\n已验证结构附件（用 ID 调用计算工具）：\n'+JSON.stringify([structure]);
  assert.deepEqual(splitReference(text),{text:'查看',context:'notes',structures:[structure]});
});

test('assistant-only result reports do not overwrite user turns or shift token usage',()=>{
  const turns=buildTurns([{role:'user',content:'原问题'},{role:'assistant',content:'原回答'},
    {role:'assistant',content:'运行结果',task_event:'e1'},{role:'user',content:'继续'},{role:'assistant',content:'新回答'}],
    [{status:'completed',started_at:'1',id:1},{status:'completed',started_at:'2',id:2}]);
  assert.deepEqual(turns.map(t=>[t.answer,t.run?.id]),[['原回答',1],['运行结果',undefined],['新回答',2]]);
  assert.equal(turns[1].event,'e1');
});

test('parallel steps follow actual child status, not the position of the last submission',async()=>{
  const {workflowPlan}=await import('../src/chemistry_agent/web/workflow-plan.mjs');
  const item={surface_plan:{co_height:1.85,co_bond:1.14},stages:[
    {stage:'bulk',title:'体相',reused_pk:60},{stage:'surface',title:'表面'},
    ...['top','bridge','hollow'].map(stage=>({stage,title:stage,parallel:true})),{stage:'energies',title:'汇总'}]};
  const job={steps:[{label:'x:surface',pk:74,finished_ok:true},{label:'x:bridge',pk:80,scheduler_state:'running'},
    {label:'x:hollow',pk:82,scheduler_job_id:'32'}]};
  const html=workflowPlan(item,job);
  assert.equal((html.match(/class="plan-group"/g)||[]).length,4);
  assert.match(html,/bridge<\/span><small>运行中/); assert.match(html,/hollow<\/span><small>已交调度器/);
  assert.match(html,/汇总<\/span><small>等待前序/); assert.doesNotMatch(html,/提出修改/);
  assert.match(workflowPlan(item,null),/提出修改/);
});
