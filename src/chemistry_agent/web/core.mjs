export const TOOL_LABELS = {
  ask_user_question: "询问用户",
  web_search: "联网搜索",
  remote_shell: "远程终端",
  molecular_properties: "分子属性计算",
  molecular_similarity: "结构相似度比较",
  calculation_setup: "服务器与程序配置",
  import_structure: "导入结构",
  build_structure: "ASE 代码建模",
  calculation_artifacts: "找回计算记录",
  calculation_artifact: "读取原始计算参数",
  calculation_prepare: "匹配方案与赝势",
  calculation_preview: "生成计算预览",
  calculation_submit_confirmed: "用户确认提交",
  calculation_job_hosts: "查询任务服务器",
  calculation_job_files: "检查作业与文件",
  calculation_plan_extend: "追加后续步骤",
  calculation_jobs: "查询计算任务",
  calculation_job: "读取状态与结果",
};

export function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[character]);
}

export function localFilePath(href) {
  try {
    const url = new URL(href);
    if (url.protocol === 'file:' && (!url.hostname || url.hostname === 'localhost'))
      return decodeURIComponent(url.pathname);
  } catch { /* Not a local file URI. */ }
  return null;
}

export function createMarkdownRenderer(factory, {katex = globalThis.katex, texmath = globalThis.texmath} = {}) {
  const renderer = factory({ html: false, breaks: true, linkify: false });
  if (katex && texmath) renderer.use(texmath, {
    // Each equation gets its own macros; no cross-message state or trusted HTML/URLs.
    engine: {renderToString: (source, options) => katex.renderToString(source, {...options, macros:{}})},
    delimiters: ['dollars', 'brackets'],
    katexOptions: {trust:false, throwOnError:false, strict:'ignore', maxExpand:1000, maxSize:20},
  });
  const validLink = renderer.validateLink.bind(renderer);
  renderer.validateLink = href => localFilePath(href) !== null || validLink(href);
  renderer.core.ruler.after('inline', 'local_file_references', state => {
    for (const token of state.tokens) {
      const stack = [];
      for (const child of token.children || []) {
        if (child.type === 'link_open') {
          const path = localFilePath(child.attrGet('href'));
          stack.push(path);
          if (path !== null) child.meta = {...child.meta, localFilePath:path};
        } else if (child.type === 'link_close') {
          const path = stack.pop();
          if (path !== null && path !== undefined) child.meta = {...child.meta, localFilePath:path};
        }
      }
    }
  });
  // CommonMark treats punctuation beside a Chinese character as intraword.
  // Relax that boundary for double asterisks, while the parser still owns
  // escaping, code spans, links, and matching nested emphasis.
  const emphasis = renderer.inline.ruler.__rules__.find(rule => rule.name === "emphasis").fn;
  renderer.inline.ruler.at("emphasis", (state,silent) => {
    const scan = state.scanDelims;
    state.scanDelims = function(start,splitWord) {
      const result = scan.call(this,start,splitWord);
      if (result.length===2 && this.src[start]==="*") {
        const before=this.src[start-1] || " ", after=this.src[start+2] || " ";
        if (/\p{Script=Han}/u.test(before+after)) {
          result.can_open ||= !/\s/.test(after);
          result.can_close ||= !/\s/.test(before);
        }
      }
      return result;
    };
    try { return emphasis(state,silent); }
    finally { state.scanDelims=scan; }
  });
  renderer.renderPartial = (source) => {
    // Balance an unfinished emphasis token for display only. Never alter the
    // source saved by the streaming transport or delimiters inside code.
    const parsed=renderer.parse(source,{});
    const inline=parsed.at(-2);
    const last=inline?.children?.at(-1);
    if (inline?.type==="inline" && last?.type==="text" && /(?:^|[^\\*])\*\*[^\s*][^*]*$/.test(last.content)
        && !source.includes("\\**") && !source.endsWith("\n")) {
      const candidate=renderer.render(source.trimEnd()+"**");
      if(candidate.includes("</strong></p>")) return candidate;
    }
    return renderer.render(source);
  };
  const original = renderer.renderer.rules.link_open;
  const originalClose = renderer.renderer.rules.link_close;
  renderer.renderer.rules.link_open = (tokens, index, options, env, self) => {
    const path = tokens[index].meta?.localFilePath;
    if (path !== undefined) return `<span class="local-file-reference" data-local-file-path="${escapeHtml(path)}" title="${escapeHtml(path)}">`;
    tokens[index].attrSet("target", "_blank");
    tokens[index].attrSet("rel", "noopener noreferrer");
    return original ? original(tokens, index, options, env, self) : self.renderToken(tokens, index, options);
  };
  renderer.renderer.rules.link_close = (tokens, index, options, env, self) => {
    if (tokens[index].meta?.localFilePath !== undefined) return '</span>';
    return originalClose ? originalClose(tokens, index, options, env, self) : self.renderToken(tokens, index, options);
  };
  // Remote images can send a user's IP or browsing metadata. Show the alt text instead.
  renderer.renderer.rules.image = (tokens, index) => escapeHtml(tokens[index].content);
  return renderer;
}

export function splitReference(content) {
  const prefix = "\n\n<user_provided_reference>\n";
  const suffix = "\n</user_provided_reference>";
  const marker = "\n\n已验证结构附件（用 ID 调用计算工具）：\n";
  const boundary = content.lastIndexOf(marker);
  const items = boundary < 0 ? null : parseJson(content.slice(boundary + marker.length));
  const attached = Array.isArray(items) && items.length && items.every(s => typeof s?.name === "string" && /^[a-f0-9-]{36}$/i.test(s.id)) ? {structures:items} : {};
  if (attached.structures) content = content.slice(0,boundary);
  const index = content.indexOf(prefix);
  if (index < 0 || !content.endsWith(suffix)) return { text: content, context: "", ...attached };
  return { text: content.slice(0, index), context: content.slice(index + prefix.length, -suffix.length), ...attached };
}

export function parseJson(value, fallback = null) {
  try { return JSON.parse(value); } catch { return fallback; }
}

export function buildTurns(messages, runs = []) {
  const turns = [];
  const completed = runs.filter((run) => run.status === "completed" && !["diagnosis","submission"].includes(run.context?.automation_event) && !run.context?.manual_compaction)
    .sort((a, b) => a.started_at.localeCompare(b.started_at));
  let turn = null;
  let pendingCalls = new Map();
  const finalAnswers = new Set(completed.map(run=>run.answer).filter(Boolean));
  for (const message of messages) {
    // A persisted background run has no synthetic user message. Keep its reply and
    // tool trace separate instead of replacing the preceding user's answer.
    if (message.role === "assistant" && !message.task_event &&
        (!turn || finalAnswers.has(turn.answer))) {
      turn = {text:"",context:"",answer:"",calls:[],steps:[],run:null,background:true};
      turns.push(turn);
      pendingCalls = new Map();
    }
    if (message.role === "assistant" && message.task_event) {
      turns.push({event:message.task_event,text:"",context:"",calls:[],answer:message.content,run:null});
      turn = null;
    } else if (message.role === "user") {
      turn = { ...splitReference(message.content || ""), guidance:message.guidance || [], answer: "", calls: [], steps: [], run: completed[turns.length] || null };
      turns.push(turn);
      pendingCalls = new Map();
    } else if (turn && message.role === "assistant") {
      if (message.tool_calls?.length) {
        if (message.content) turn.steps.push({type:'commentary', text:message.content});
        for (const call of message.tool_calls) {
          pendingCalls.set(call.id, call);
          turn.steps.push({type:'tool',call_id:call.id,name:call.function?.name || '工具',status:'uncertain'});
        }
      } else if (message.content) turn.answer = message.content;
    } else if (turn && message.role === "tool") {
      const call = pendingCalls.get(message.tool_call_id);
      const result = parseJson(message.content, { error: "invalid_result", message: "无法读取结果" });
      turn.calls.push({ call_id: message.tool_call_id, name: call?.function?.name || "工具",
        arguments: parseJson(call?.function?.arguments || "{}", {}),
        result, success: result !== null && !result.error });
      const step = turn.steps.find(item=>item.call_id===message.tool_call_id);
      if (step) step.status = result !== null && !result.error ? 'completed' : 'failed';
    }
  }
  // The latest run page contains the latest completed turns, even with failed runs mixed in.
  const chatTurns = turns.filter(item=>!item.event);
  const offset = chatTurns.length - completed.length;
  chatTurns.forEach((item, index) => { item.run = offset >= 0 ? completed[index - offset] || null : null; });
  for (const run of runs.filter(row=>['running','failed','cancelled'].includes(row.status) && row.context?.automation_event==='job_completion')) {
    const answer = run.status === 'running' ? run.answer || '后台 Agent 正在准备计算。'
      : run.status === 'cancelled' ? '后台 Agent 处理已中断。'
      : `后台 Agent 未完成处理：${String(run.context?.error_message || run.error_code || '连接失败').replace(/[。.!！]+$/u,'')}。计算记录已保留。`;
    const background = {text:'',context:'',answer,
      calls:run.tool_calls || [],steps:run.context?.public_steps || [],run,background:true};
    const next = turns.findIndex(turn=>turn.run?.started_at > run.started_at);
    if (next < 0) turns.push(background); else turns.splice(next,0,background);
  }
  return turns;
}

export function metricValue(value, digits = 4) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return Number.isInteger(value) ? String(value) : String(Number(value.toFixed(digits)));
}
