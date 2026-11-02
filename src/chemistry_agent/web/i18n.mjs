// Translate presentation only. User messages, names, file contents and model output are excluded.
const messages = {
  "步骤失败":"Step failed", "全屏缩略导航":"Minimap in full screen", "缩略图导航":"Minimap navigation", "点击或拖动缩略图定位":"Click or drag to navigate", "可见节点 / 总节点":"Visible / total nodes", "拖动卡片移动，拖动空白平移":"Drag a card to move it, or drag the paper to pan",
  "方案画布":"Plan canvas", "全屏方案画布":"Full-screen plan canvas", "计算依赖图":"Dependency map",
  "查找步骤":"Find step", "查找名称或步骤 ID":"Find by name or step ID", "没有匹配的步骤":"No matching steps",
  "画布选项":"Canvas options", "全屏画布":"Expand canvas", "退出全屏":"Exit full screen", "适应画布":"Fit to view",
  "按分支查看":"Focus a branch", "全部分支":"All branches", "显示建模与分析步骤":"Include modeling and analysis",
  "展开分支":"Expand branch", "收起分支":"Collapse branch", "分支概览":"Branch overview", "展开全部节点":"Expand all nodes",
  "恢复自动布局":"Reset card positions", "快捷键":"Shortcuts", "画布快捷键":"Canvas shortcuts", "← 收起 · → 展开":"← Fold · → Unfold",
  "收起 / 展开分支":"Fold / unfold branch", "上一个 / 下一个节点":"Previous / next node", "查看步骤详情":"Open step details",
  "切换全屏":"Toggle full screen", "关闭浮层或全屏":"Close menu or full screen", "调整画布高度":"Resize canvas height",
  "拖动调整画布高度":"Drag to resize canvas", "步骤导航":"Step navigation", "返回画布":"Back to canvas", "全屏详情":"Full-screen details",
  "步骤详情":"Step details", "步骤参数":"Step parameters", "结构文件":"Structure files", "依赖关系":"Dependencies",
  "直接上游":"Inputs from", "直接下游":"Used by", "结果与下载":"Results and files", "暂无可查看的结构文件":"No structure files available",
  "结构建模":"Structure modeling", "数据分析":"Data analysis", "计算步骤":"Calculation step", "VASP 计算":"VASP calculation",
  "能量收敛曲线":"Energy convergence", "排查记录":"Investigations", "执行代码":"Source code", "提出修改":"Request changes",
  "消息":"Messages", "进度":"Progress", "暂无步骤回复":"No step updates", "暂无消息。":"No messages.",
  "调整右栏宽度":"Resize inspector", "最大化面板":"Maximize inspector", "还原面板":"Restore inspector",
  "最大化或还原面板":"Maximize or restore inspector", "缩小方案":"Zoom out", "放大方案":"Zoom in",
  "还原缩放":"Reset zoom", "全屏":"Full screen", "上下文":"Context", "查看上下文用量":"View context usage",
  "当前会话":"Current chat", "关闭消息":"Close messages", "步骤进度与解读":"Step progress and agent updates",

  "计算":"Calculations", "结构":"Structures", "工具":"Tools", "此对话暂无计算记录。":"No calculations in this conversation yet.", "结构查看器":"Structure viewer",
  "高级选项":"Advanced options",
  "连接后自动读取可用模型。保持自动选择即可使用，也可从列表切换。":"Available models load automatically after connecting. Keep Automatic or choose from the list.",
  "授权 Chemistry Agent 使用你的 ChatGPT 套餐，无需 API Key。首次授权的应用名称为 Chemistry Agent，可在 ChatGPT 用量设置中管理。": "Authorize Chemistry Agent to use your ChatGPT plan without an API key. Manage its access in ChatGPT Usage settings.",
  "请在运行 Chemistry Agent 的电脑上完成浏览器授权。已保存的账号可切换或重新授权；应用不会读取你日常 Codex 的凭据。": "Complete browser authorization on the computer running Chemistry Agent. Switch or reauthorize saved accounts. Your everyday Codex credentials are not read.",
  "添加 ChatGPT 账号": "Add ChatGPT account",
  "ChatGPT 账号": "ChatGPT account",
  "选择已保存的账号": "Choose a saved account",
  "管理 ChatGPT 应用用量 ↗": "Manage ChatGPT app usage \u2197",
  "兼容旧 Codex 登录": "Legacy Codex sign-in",
  "此方式仍显示 Codex 名称，供已有连接和设备码登录使用。": "This option still appears as Codex and supports existing connections and device-code login.",
  "默认模型": "Default model",
  "自动选择": "Automatic",
  "请选择模型": "Choose a model",
  "刷新模型": "Refresh models",
  "对话模型": "Chat model",
  "跟随默认模型": "Use default model",
  "手动填写模型 ID": "Enter model ID manually",
  "应用到此对话": "Apply to this chat",
  "留空自动选择可用模型": "Leave blank to choose an available model automatically",
  "可从列表选择，也可手动填写模型 ID。聊天输入框旁可为每段对话单独选择模型。": "Choose from the list or enter a model ID. You can also select a model for each chat beside the composer.",
  "请先保存连接设置，再刷新模型列表。": "Save the connection settings before refreshing models.",
  "正在读取模型列表…": "Loading models\u2026",
  "模型列表已更新；实际可用性以连接测试为准。": "Model catalog updated. Test the connection to verify access.",
  "未返回模型，可手动填写模型 ID。": "No models returned. You can enter a model ID manually.",
  "当前使用旧 Codex 连接。点击 Continue with ChatGPT 可授权 Chemistry Agent 独立身份。": "Using the legacy Codex connection. Continue with ChatGPT to authorize Chemistry Agent separately.",
  "Chemistry Agent 已获 ChatGPT 授权，可以选择模型并测试连接。": "Chemistry Agent is authorized. Choose a model and test the connection.",
  "登录失败，请重新授权。": "Sign-in failed. Please authorize again.",
  "已退出本地登录，远端撤销未确认；可在 ChatGPT 用量设置中断开应用。": "Signed out locally; remote revocation was not confirmed. Disconnect the app in ChatGPT Usage settings.",
  "请先在设置中登录 ChatGPT。": "Sign in with ChatGPT in settings first.",

  "Chemistry Agent 首页":"Chemistry Agent home", "设置":"Settings", "常规":"General", "外观":"Appearance", "模型":"Models", "语言":"Language",
  "选择界面显示语言。":"Choose the interface language.", "研究空间":"Workspace", "返回研究空间":"Back to workspace",
  "设置分类":"Settings sections", "工作空间账户":"Workspace account", "正在读取…":"Loading…", "本地账户":"Local account",
  "本地单用户模式":"Local single-user mode", "个人工作空间 · 已登录":"Personal workspace · Signed in", "退出登录":"Sign out",
  "显示模式":"Display mode", "跟随系统":"System", "白色":"Light", "深夜":"Dark", "强调色":"Accent color",
  "靛蓝":"Indigo", "雾蓝":"Slate blue", "灰紫":"Mauve", "暖棕":"Warm brown", "自定义强调色":"Custom accent color",
  "已保存到当前浏览器。":"Saved in this browser.", "已应用；浏览器禁止存储，刷新后可能恢复默认。":"Applied for this visit; browser storage is unavailable.",
  "模型连接":"Model connection", "选择账号授权或 API 服务，保存后立即生效。":"Choose an account or API service. Changes take effect after saving.",
  "登录方式":"Provider", "ChatGPT 账号授权登录":"ChatGPT account", "OpenAI 兼容 API / 本地模型":"OpenAI-compatible API / local model",
  "使用 ChatGPT 订阅":"ChatGPT subscription", "由官方 Codex 客户端完成授权，使用账号可用的 Codex 额度。无需填写 OpenAI API Key。":"Authorize with the official Codex client using your account's available Codex allowance. No API key is needed.",
  "正在检查登录状态…":"Checking sign-in status…", "使用 ChatGPT 登录":"Sign in with ChatGPT", "使用设备码登录":"Use a device code", "退出 ChatGPT":"Sign out of ChatGPT",
  "打开官方授权页面 ↗":"Open authorization page ↗", "设备码：":"Device code:", "取消登录":"Cancel sign-in",
  "浏览器登录适合在运行服务的电脑上使用；远程访问请选择设备码。设备码登录需要在 ChatGPT 安全设置中启用。登录仅用于本研究空间，不会退出你日常使用的 Codex。":"Use browser sign-in on the service computer, or a device code for remote access. Enable device-code login in ChatGPT security settings. This workspace uses a separate Codex sign-in.",
  "使用自己的 API":"Use your own API", "服务地址（Base URL）":"Base URL",
  "填写接口根地址，不要添加 /chat/completions。兼容模式支持提供 Chat Completions 工具调用的服务，本地 HTTP 模型可留空密钥。":"Enter the API root URL without /chat/completions. Compatible services must support Chat Completions tool calls. Local HTTP models may omit the API key.",
  "输入 API Key":"Enter API key", "删除已保存的 API Key":"Delete saved API key", "启用 DeepSeek 思考模式":"Enable DeepSeek thinking",
  "模型 ID":"Model ID", "（可选）":"(optional)", "留空使用 Codex 默认模型":"Leave blank for the Codex default", "填写服务商提供的模型 ID":"Enter the provider's model ID",
  "可填写当前账号支持的 Codex 模型 ID。":"Optionally enter a Codex model ID available to your account.",
  "模型需要支持 Chat Completions 及工具调用，才能进行自动建模和计算。":"The model must support Chat Completions and tool calling for automated modeling and calculations.",
  "保存设置":"Save settings", "保存并测试连接":"Save and test connection",
  "连接测试会向所选服务发送一条简短消息，可能消耗额度。API Key 仅保存在服务端当前用户的私有配置文件中，不写入浏览器存储。":"Testing sends a short message and may use your allowance. API keys are stored privately on the server, never in browser storage.",
  "已保存密钥。留空可保留；更换服务地址或类型时须重新填写。":"A key is saved. Leave blank to keep it; enter a new key when changing the provider or URL.",
  "尚未保存 API Key。":"No API key saved.", "更换服务时，请填写对应 API Key。":"Enter the API key for the selected service.",
  "请先登录研究空间。":"Please sign in to the workspace.", "请完整填写模型设置。":"Complete the model settings.",
  "设置未保存，请检查输入格式。也请确认远程服务使用 HTTPS。":"Settings were not saved. Check the values and use HTTPS for remote services.",
  "无法连接服务，请稍后重试。":"Unable to connect. Please try again.", "正在启动授权…":"Starting authorization…", "请在官方页面完成授权。":"Complete authorization on the official page.",
  "授权完成。":"Authorization complete.", "登录失败，请重试。设备码模式需在 ChatGPT 安全设置中启用。":"Sign-in failed. Enable device-code login in ChatGPT security settings if needed.",
  "授权已超时，请重新登录。":"Authorization expired. Sign in again.", "已取消登录。":"Sign-in cancelled.", "尚未登录 ChatGPT。":"Not signed in to ChatGPT.", "尚未登录。":"Not signed in.",
  "服务电脑未找到 Codex CLI。安装后重启服务即可使用账号登录。":"Codex CLI was not found on the service computer. Install it and restart the service to use account sign-in.",
  "已登录 ChatGPT，可以保存设置并测试连接。":"Signed in to ChatGPT. Save settings and test the connection.",
  "已保存。返回研究空间即可使用新的模型设置。":"Saved. Return to the workspace to use the new model.", "设置已保存，正在测试 AI 响应…":"Saved. Testing the model response…",
  "已启动 ChatGPT 授权，请完成官方登录页面上的操作。":"Authorization started. Complete sign-in on the official page.", "已退出本研究空间的 ChatGPT 登录。":"Signed out of ChatGPT for this workspace.",
  "已删除保存的 API Key，并切换为 ChatGPT 登录方式。":"API key deleted. Switched to ChatGPT sign-in.",
  "折叠侧栏":"Collapse sidebar", "展开侧栏":"Expand sidebar", "新建对话":"New chat", "新对话":"New chat", "＋ 新对话":"＋ New chat", "结构与轨迹":"Structures & trajectories",
  "项目":"Projects", "聊天":"Chats", "项目与聊天":"Projects & chats", "添加文件":"Add files", "上传文件":"Upload files",
  "新建项目":"New project", "项目文件夹":"Project folders", "按项目整理相关对话":"Organize conversations into projects", "暂无独立聊天":"No chats yet", "暂无对话":"No conversations yet",
  "刷新会话":"Refresh chats", "历史会话":"Chat history", "加载更多对话":"Load more", "会话导航":"Navigation", "关闭侧栏":"Close sidebar", "打开会话导航":"Open navigation",
  "打开研究工具":"Open research tools", "关闭研究面板":"Close research panel", "输入化学问题":"Message", "输入问题，或描述你的研究任务…":"Ask a question or describe your research task…", "输入问题，开始研究。":"Ask a question to get started.",
  "模型设置":"Model settings", "模型未配置":"Set up a model", "服务未连接":"Service offline", "正在连接…":"Connecting…", "已登录":"Signed in", "已配置":"Connected", "发送消息":"Send message", "正在生成回答":"Generating a response",
  "研究面板":"Research panel", "研究工具与计算结果":"Research tools and results", "分子工具":"Molecular tools", "本地计算":"Local",
  "分子 · 晶胞 · 轨迹":"Molecules · cells · trajectories", "打开查看器 ↗":"Open viewer ↗", "分子属性":"Molecular properties", "分子量 · LogP · 氢键 · TPSA":"Mass · LogP · H-bonds · TPSA",
  "结构相似度":"Structure similarity", "Morgan 指纹 · Tanimoto":"Morgan fingerprint · Tanimoto", "分子 SMILES":"Molecule SMILES", "例如 CCO（乙醇）":"e.g. CCO (ethanol)",
  "计算分子属性":"Calculate properties", "分子 A · SMILES":"Molecule A · SMILES", "分子 B · SMILES":"Molecule B · SMILES", "例如 CCO":"e.g. CCO", "例如 CO":"e.g. CO", "比较结构相似度":"Compare structures",
  "工具结果":"Tool results", "暂无记录":"No results", "运行工具后，在这里查看结果。":"Run a tool to view results here.", "已计算":"Calculated", "请检查输入":"Check input",
  "分子量":"Molecular weight", "精确质量":"Exact mass", "氢键供体":"H-bond donors", "氢键受体":"H-bond acceptors", "可旋转键":"Rotatable bonds", "环数量":"Rings", "查看参数与完整结果":"Parameters and full result",
  "项目名称":"Project name", "例如：CO₂ 还原机理":"e.g. CO₂ reduction", "将相关对话收纳在同一个项目中。":"Keep related conversations in one project.", "删除文件夹会保留所有对话，并将它们移回聊天列表。":"Deleting this folder keeps its conversations in Chats.", "删除文件夹":"Delete folder", "取消":"Cancel", "保存":"Save", "管理项目":"Manage project", "请输入项目名称。":"Enter a project name.", "已删除文件夹，对话保留在聊天列表。":"Folder deleted. Conversations are kept in Chats.",
  "正在上传…":"Uploading…", "文本可读取":"Text available", "已上传，未解析":"Uploaded, not parsed", "移除附件":"Remove attachment", "每条消息最多附加 10 个文件。":"Attach up to 10 files per message.", "单个文件不能超过 25 MB。":"Each file must be 25 MB or smaller.", "文件上传失败，请重试。":"Upload failed. Please try again.",
  "附件与上下文":"Attachments & context", "复制回答":"Copy response", "回答已复制。":"Response copied.", "复制不可用，请选中文字后手动复制。":"Select the text and copy it manually.", "重试这条消息":"Retry message", "对话已保存":"Saved", "研究助手":"Research assistant",
  "正在打开这段研究对话…":"Opening conversation…", "正在分析问题…":"Thinking…", "请先连接并配置模型。":"Connect a model first.", "请输入有效的 SMILES。":"Enter valid SMILES.", "正在计算…":"Calculating…", "计算完成，结果已显示在研究面板。":"Calculated. Results are in the research panel.",
  "查看结果":"View result", "分子属性计算":"Molecular properties", "结构相似度比较":"Structure similarity", "上下文已压缩":"Context compacted", "工具结果已精简":"Tool results compacted",
  "计算未完成，请检查输入的分子结构。":"Calculation failed. Check the molecule input.", "暂时无法读取会话，请点击刷新。":"Unable to load chats. Try refreshing.",
  "暂时无法连接本地服务，请检查服务是否正在运行。":"Cannot connect to the local service. Check that it is running.", "登录已过期，请重新登录。":"Session expired. Sign in again.", "输入格式有误，请检查后再试。":"Check your input and try again.", "请求未能完成，请稍后重试。":"The request failed. Please try again.",
  "正在打开研究空间…":"Opening workspace…", "登录工作空间":"Sign in to workspace", "使用你的工作空间账号登录。":"Use your workspace account to sign in.", "用户名":"Username", "例如 researcher":"e.g. researcher", "密码":"Password", "请输入密码":"Enter your password", "账号由管理员开通。需要帮助时，请联系工作空间管理员。":"Accounts are created by your workspace administrator.", "暂时无法连接服务，请稍后刷新页面重试。":"Unable to connect. Refresh the page to try again."
};
let language = "zh-CN";
try { language = localStorage.getItem("chemistry.language") === "en" ? "en" : "zh-CN"; } catch { /* Optional storage. */ }
export const getLanguage = () => language;
export function t(value) {
  if (language !== "en" || typeof value !== "string") return value;
  const trimmed = value.trim();
  if (messages[trimmed]) return value.replace(trimmed, messages[trimmed]);
  return value.replace(/ · 已配置$/, " · Connected").replace(/ · 已登录$/, " · Signed in")
    .replace(/^(\d[\d,]* tokens · )对话已保存$/, "$1Saved")
    .replace(/^已使用 (\d+) 次研究工具(?: · (\d+) 次需调整输入)?$/, (_, n, failed) => `Used ${n} research tools${failed ? ` · ${failed} need attention` : ""}`);
}
const ignored = 'script,style,pre,code,.markdown,[data-no-i18n],.session-button,.project-folder>summary>span,#conversation-title,input,textarea';
const originals = new WeakMap();
function translateNode(node) {
  if (!node.parentElement || node.parentElement.closest(ignored)) return;
  let record = originals.get(node);
  if (!record || node.nodeValue !== record.output) record = { source: node.nodeValue };
  record.output = t(record.source);
  if (node.nodeValue !== record.output) node.nodeValue = record.output;
  originals.set(node, record);
}
const attributeOriginals = new WeakMap();
export function translateUI() {
  document.documentElement.lang = language;
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) translateNode(walker.currentNode);
  for (const element of document.querySelectorAll('[placeholder],[aria-label],[title]')) {
    if (element.closest('[data-no-i18n],.session-button,.project-folder>summary>span,.markdown')) continue;
    const records = attributeOriginals.get(element) || {};
    for (const attribute of ['placeholder','aria-label','title']) {
      const value = element.getAttribute(attribute);
      if (value === null) continue;
      let record = records[attribute];
      if (!record || value !== record.output) record = {source:value};
      record.output = t(record.source);
      if (value !== record.output) element.setAttribute(attribute,record.output);
      records[attribute] = record;
    }
    attributeOriginals.set(element, records);
  }
  document.title = document.querySelector('[data-panel]') ? `${t('设置')} · Chemistry Agent` : 'Chemistry Agent';
}
export function setLanguage(value) {
  language = value === 'en' ? 'en' : 'zh-CN';
  try { localStorage.setItem('chemistry.language',language); localStorage.setItem('structure-viewer.language',language); } catch { /* Apply for this visit. */ }
  translateUI(); window.dispatchEvent(new Event('languagechange'));
}
if (typeof document !== 'undefined') {
  const observer = new MutationObserver(() => translateUI());
  translateUI();
  observer.observe(document.body,{childList:true,subtree:true,characterData:true,attributes:true,attributeFilter:['placeholder','aria-label','title']});
  window.addEventListener('storage',(event) => { if (event.key === 'chemistry.language') { language=event.newValue==='en'?'en':'zh-CN'; translateUI(); window.dispatchEvent(new Event('languagechange')); } });
}
