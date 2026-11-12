import {activity, duration} from "../src/chemistry_agent/web/answer-ui.mjs";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";
import { restoreDockSelection } from "../src/chemistry_agent/web/inspector-tabs.mjs";
import { buildTurns, createMarkdownRenderer, escapeHtml, metricValue, splitReference } from "../src/chemistry_agent/web/core.mjs";

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

test("background job replies preserve the original answer and their own tool traces", () => {
  const messages = [
    {role:"user",content:"优化"}, {role:"assistant",content:"方案已准备"},
    {role:"assistant",tool_calls:[{id:"t",function:{name:"job.inspect",arguments:"{}"}}]},
    {role:"tool",tool_call_id:"t",content:'{"state":"COMPLETED"}'},
    {role:"assistant",content:"优化完成"},
    {role:"assistant",content:"方案完成",task_event:"plan:test"},
    {role:"assistant",content:"汇总完成"},
  ];
  const runs = ["方案已准备","优化完成","汇总完成"].map((answer,index)=>({
    id:String(index),status:"completed",started_at:`2026-01-0${index+1}`,answer,
    context:index ? {automation_event:"job_completion"} : {},
  }));
  const turns=buildTurns(messages,runs);
  assert.equal(turns.length,4);
  assert.equal(turns[0].answer,"方案已准备");
  assert.equal(turns[0].calls.length,0);
  assert.equal(turns[1].background,true);
  assert.equal(turns[1].calls[0].name,"job.inspect");
  assert.equal(turns[1].run.id,"1");
  assert.equal(turns[2].event,"plan:test");
  assert.equal(turns[3].answer,"汇总完成");
});

test("turn activity uses real run time and keeps tool parameters inside the disclosure", () => {
  const start="2026-11-03T09:00:00Z", end="2026-11-03T09:01:06Z";
  assert.equal(duration(start,end),"1 分 6 秒");
  const content=activity({id:"run",start,end,calls:[{name:"test",success:false,arguments:{x:"<img>"}}]});
  assert.match(content,/用时 1 分 6 秒/);
  assert.match(content,/&lt;img&gt;/);
  assert.doesNotMatch(content,/查看结果|已保存/);
  assert.equal(activity({id:"legacy"}),"");
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

test('general graph shows true dependencies, independent layers and reviewed revisions',async()=>{
  const {graphLayers,renderGeneralPlan,generalRows}=await import('../src/chemistry_agent/web/general-plan.mjs');
  const nodes=[{id:'bulk',title:'体相',depends_on:[],inputs:{},kind:'model'},
    {id:'clean',title:'洁净表面',depends_on:['bulk'],inputs:{bulk:{node:'bulk',output:'structure'}},kind:'model'},
    ...['top','bridge'].map(id=>({id,title:id,depends_on:['clean'],inputs:{slab:{node:'clean',output:'structure'}},kind:'model'})),
    {id:'sum',title:'汇总',depends_on:['top','bridge'],inputs:{},kind:'analysis'}];
  assert.deepEqual(graphLayers(nodes).map(layer=>layer.map(n=>n.id)),[['bulk'],['clean'],['top','bridge'],['sum']]);
  const plan={id:'id',title:'CO <test>',goal:'比较',revision:2,status:'draft',max_parallel:2,nodes,
    runtime:Object.fromEntries(nodes.map(n=>[n.id,{status:'pending'}])),changed_nodes:['bridge'],history:[{revision:2,changed_nodes:['bridge'],removed_nodes:[]}]};
  const html=renderGeneralPlan(plan);
  assert.match(html,/独立分支 · 可并行/);assert.match(html,/依赖：洁净表面/);
  assert.match(html,/已修改 · 待确认/);assert.match(html,/data-confirm-plan/);
  assert.doesNotMatch(html,/<test>/);
  assert.match(generalRows(plan,nodes[1]),/实际输出结构/);
  assert.doesNotMatch(renderGeneralPlan({...plan,status:'running'}),/data-confirm-plan/);
  assert.throws(()=>graphLayers([{id:'a',depends_on:['b']}]),/依赖/);
});

test('stream previews can complete a strong span without changing code or exponentiation',()=>{
  assert.match(markdown.render('比较**“吸附能”**结果'), /<strong>“吸附能”<\/strong>/);
  assert.match(markdown.renderPartial('正在读 **OUTCAR'), /<strong>OUTCAR<\/strong>/);
  assert.equal(markdown.renderPartial('2 ** 3'),markdown.render('2 ** 3'));
  assert.equal(markdown.renderPartial('`**partial`'),markdown.render('`**partial`'));
});

test("the empty research panel stays empty until a tool is chosen", () => {
  const available = new Set(["calculation", "structure", "events", "tools"]);
  const empty = {tabs:[], tab:null};
  assert.deepEqual(restoreDockSelection(null, available), empty);
  const oldDefault = {tabs:["calculation"], tab:"calculation"};
  assert.deepEqual(restoreDockSelection(oldDefault, available), empty);
  assert.deepEqual(restoreDockSelection({...oldDefault, version:2}, available), oldDefault);
  const olderLayout = {tabs:["tools", "unknown", "structure", "tools"], tab:"structure"};
  assert.deepEqual(restoreDockSelection(olderLayout, available), {tabs:["tools", "structure"], tab:"structure"});
});
