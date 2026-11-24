# Chem Code

Chem Code 是面向计算催化研究的聊天工作区。用户可以给出结构，与 Agent 商定计算方案，在侧栏检查每一步的输入、依赖和结果，通过通用 Slurm 作业监控和持久事件唤醒推进计算。默认运行不需要 AiiDA。

当前开发分支、已合入的功能、待验收事项和旧草稿恢复位置见[项目进度与分支维护](docs/project-status.md)。

可在 Codex 中单独使用的研究方案与 Slurm 作业监控插件由[独立插件仓库](https://github.com/liyang-tju/chem-research-plugins)维护。

## 功能总览

| 工作环节 | 当前可用功能 |
| --- | --- |
| 研究对话 | ChatGPT 账号授权、DeepSeek 和 OpenAI 兼容 API、按会话选模型、流式回复、工具调用和历史压缩。聊天请求持久化，刷新后可回到原会话。 |
| 方案讨论 | Agent 通过 `ask_user_question` 提出会影响方案的选择。支持单选、多选和文字补充。回复后继续设计，计算提交使用独立的预览确认或本轮自动执行授权。 |
| 研究工具 | 多文件上传、公开网页搜索、RDKit 分子属性与相似度计算。用户可在授权的聊天轮次通过 SSH 查询已登记的服务器。 |
| 建模与查看 | Agent 编写 ASE 代码生成结构；可复用已保存结构或上游实际末态，保留受支持的固定原子和方向约束。查看器支持结构和轨迹的播放、测量、晶胞显示与快照导出。 |
| 计算环境 | 读取本机 SSH 配置；Agent 查找远端软件、环境脚本和赝势，按需求编写输入与提交脚本。默认不需要 AiiDA 或软件插件。 |
| 多步计算 | Agent 用研究任务、通用 Slurm 作业、建模和 Python 分析节点编写可修订的依赖图。确认后，就绪分支可并行提交；后续节点读取上游输出。方案和步骤详情可查看输入、初末态、可用的优化轨迹和能量变化。 |
| 监控与结果 | 区分方案、调度器和科学收敛状态。完成或失败事件关联原会话；可停止方案，按配置诊断重试。队列配额不足时等待已接单作业结束。分析节点可读取计算文件、汇总数据并用 Matplotlib 生成图表。 |
| 工作区与部署 | 项目与会话管理、可折叠的研究面板、中英文和明暗主题。远端账号部署支持会话隔离与备份；SSH 计算工具面向本地单用户服务。 |
| 共享知识与多服务器 | 同一方案可选择本机、SSH 或 HTTP Runner；跨服务器文件依赖校验。OpenViking 知识连接、统一 Skill 插件安装与版本管理，以及外部 Agent 的共享 MCP 接口。 |

通用作业当前使用 Slurm；PBSPro/OpenPBS 和 Torque 尚未接入通用监控。结构与轨迹支持的格式见[查看器说明](docs/structure-viewer.md)，远端部署见[部署说明](DEPLOYMENT.md)。

本地/常驻服务器部署、OpenViking 和 Skill 插件接入见[多服务器与共享知识](docs/RESEARCH_ENVIRONMENTS.md)。

## 模型登录

研究空间左下角的「设置 → 模型」支持 ChatGPT 账号授权、DeepSeek API Key 和自定义 OpenAI
兼容服务。配置保存后立即生效，不需要提前填写密钥。安装、授权和接口范围见
[模型登录与配置](docs/MODEL_LOGIN.md)。

## 安装与启动（Windows PowerShell）

在本目录打开 PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -c constraints.txt -e ".[dev]"
.\.venv\Scripts\python.exe .\scripts\setup_tokenizer.py
Copy-Item .env.example .env
```

编辑 `.env` 中的 `DEEPSEEK_API_KEY`，然后启动：

```powershell
.\.venv\Scripts\python.exe -m chemistry_agent
```

也可以用当前终端的环境变量覆盖 `.env` 配置，例如 `$env:DEEPSEEK_MODEL = "deepseek-v4-pro"`。

打开 <http://127.0.0.1:8765/> 使用前端页面；<http://127.0.0.1:8765/docs> 保留交互式 API 文档。服务默认只监听本机。没有 Key 时页面会提示配置模型，右侧分子工具仍可使用；调用 `/chat` 会明确返回 `503 model_not_configured`。

## 前端页面

- 左侧分别显示项目文件夹与独立聊天；支持创建、重命名、删除项目和在项目内新建对话。删除文件夹保留对话，项目归属按账户保存在 SQLite 中。
- 会话行采用紧凑布局；刷新后恢复上次打开的会话，也可通过页面的会话链接返回。
- 左侧栏可折叠为单列图标；左下角「设置」直接打开常规、外观、模型三个独立分类。常规支持简体中文/English；外观使用跟随系统、白色、深夜三个选择卡片，并可调整对话字号。语言、主题和侧栏折叠状态保存在当前浏览器。
- 中间输入问题，Enter 发送，Shift + Enter 换行；支持中文输入法，不会在选字时发送。
- 在对话输入框左侧点「+ → 上传文件」，可以多选任意类型。原文件按账户流式保存，可从历史对话下载。文本给模型的摘录上限为每个文件 20,000 字符、每轮 40,000 字符；结构文件可以直接预览，计算使用经过校验的会话结构 ID。
- 圆形发送按钮左侧可为当前对话选择模型、刷新目录或进入模型设置。默认模型在设置页保存，独立 ChatGPT 授权以 Chem Code 名称注册。模型回答支持 Markdown、列表、表格和代码块。
- 右侧结构查看器、分子属性、相似度与工具结果均为可折叠模块，默认收起表单。展开后可直接输入 SMILES 计算，无需调用模型；对话中产生的工具结果也会显示在此处。
- 右侧研究面板是可伸缩的内嵌布局：展开时与聊天并排，收起后不保留窄栏，开关状态自动记忆。手机窄屏切换到面板内容页，关闭后返回聊天，不使用浮层遮挡。
- 字号按层级设置：聊天正文和输入框 16px，主要导航与控件约 15px，辅助信息 13–14px，标题 19–26px。
- 对话内展开工具记录查看参数与结果，支持复制回答。失败时保留问题与文献片段，可手动重试。
- 点击回复上方的用时可展开工具记录。上下文用量和摘要可从输入框右侧的圆环查看。
- 窄屏下，会话导航和研究面板通过顶部按钮打开。界面包含键盘焦点管理及减少动画偏好支持。

前端是无构建步骤的 HTML / CSS / JavaScript，由 FastAPI 同源提供，不需要额外启动 Node 服务。静态文件位于 `src/chemistry_agent/web/`；Markdown 渲染使用从原项目复制的本地 markdown-it 资源，其 MIT 许可证保存在 `web/vendor/markdown-it-LICENSE.txt`。模型输出中的原始 HTML 不执行，远程图片不加载，Key 不会传入前端。

## 调用示例

无需 Key 即可运行分子工具：

```powershell
$body = @{ arguments = @{ smiles = "CCO" } } | ConvertTo-Json
Invoke-RestMethod http://127.0.0.1:8765/tools/molecular_properties/invoke `
  -Method Post -ContentType "application/json; charset=utf-8" -Body ([Text.Encoding]::UTF8.GetBytes($body))
```

配置 Key 后调用 agent：

```powershell
$body = @{ message = "请用工具计算乙醇 CCO 的分子式、分子量和 LogP。" } | ConvertTo-Json
$reply = Invoke-RestMethod http://127.0.0.1:8765/chat `
  -Method Post -ContentType "application/json; charset=utf-8" -Body ([Text.Encoding]::UTF8.GetBytes($body))
$reply

# 传入返回的 session_id 继续同一会话
$body = @{ session_id = $reply.session_id; message = "再比较它和甲醇 CO 的结构相似度。" } | ConvertTo-Json
Invoke-RestMethod http://127.0.0.1:8765/chat `
  -Method Post -ContentType "application/json; charset=utf-8" -Body ([Text.Encoding]::UTF8.GetBytes($body))
```

可以额外传 `context` 字符串提供文献片段；片段会随该轮用户消息一起保存在本地会话中，并发送到 DeepSeek。首次 `/chat` 不传 `session_id` 时自动建会话；传入不存在的 ID 返回 404。

| 接口 | 用途 |
| --- | --- |
| `GET /` | 聊天前端页面 |
| `GET /health` | 服务状态、是否已配置 Key、模型名称；不请求 DeepSeek |
| `GET /tools` | 工具说明和参数定义 |
| `POST /tools/{name}/invoke` | 直接执行本地工具，不调用模型 |
| `POST /sessions` | 创建会话，支持 `title` |
| `GET /sessions?limit=20&offset=0` | 查看会话列表 |
| `GET /sessions/{id}` | 查看已完成轮次的完整消息和工具消息；不返回模型推理内容 |
| `POST /chat` | 多轮对话，返回答案、会话 ID、运行 ID、工具结果和累计 token 用量 |
| `GET /runs/{id}` | 查看运行状态、工具结果和错误码 |
| `GET /sessions/{id}/runs` | 按会话查看运行列表，支持 `limit` 和 `offset` |

## 工具

- `molecular_properties`：输入 `smiles`，计算分子式、分子量、精确质量、Crippen LogP、TPSA、氢键供受体数、可旋转键数、环数和形式电荷。数值包含单位和 RDKit 版本来源。
- `molecular_similarity`：输入 `smiles_a`、`smiles_b`，计算 Morgan 指纹（radius=2，2048 bits，不含手性）的 Tanimoto 相似度。该指标不代表生物活性或反应可行性。

新增工具时在 `src/chemistry_agent/tools.py` 中定义 Pydantic 参数模型、计算函数，并注册 `Tool`。模型只可调用已注册工具，参数会在本地校验。

## 上下文压缩

每一次模型请求前都会检查 token 预算，包括工具调用后的继续请求。只压缩发送给模型的副本，SQLite 中的用户正文、助手回复、完整工具结果和思考模式协议字段都不被覆盖或删除。

默认策略：

1. ChatGPT 订阅按本轮所选模型的目录窗口计算，使用有效比例（未声明时留出 5% 余量），不采用实验性最大窗口。其他接口或窗口未知时，默认总窗口为 32,768 tokens，预留 `DEEPSEEK_MAX_TOKENS`（默认 4,096）作为回答空间，此默认配置的可用输入预算为 28,672。
2. 预计输入超过可用预算的 80%（默认约 22,937）时，将较早的完整对话轮次生成摘要；优先保留最近 4 个已完成轮次；空间不足时可摘要所有已完成轮次，当前任务保留。
3. 摘要增量更新，保存其覆盖的原始消息数量；后续对话与重启后都可复用，不会每次重写全部历史。过长的摘要输入分块处理，每个摘要请求也检查输入和输出预算。
4. 发送给模型的单份工具结果超过 512 tokens 时，保留关键属性、数值和方法，省略过长内容，并明确标记 `_context_compacted`。分子标识符不会被截成不完整结构；原始结果仍可在会话及运行记录中查看。
5. 不拆散 assistant 工具调用与对应 tool 返回，也不截断近期消息中的 DeepSeek 思考协议字段。提供给摘要模型的材料不包含私有推理内容。

摘要是有损记忆，提示词要求保留目标、约束、SMILES、数值、单位、来源、纠错与未解决问题；精确细节仍应核对原文。摘要作为低权限的历史资料发送，不作为系统指令。

`scripts/setup_tokenizer.py` 从 [DeepSeek 官方文档](https://api-docs.deepseek.com/zh-cn/quick_start/token_usage/) 所链接的地址下载 tokenizer 数据，校验固定 SHA256，仅保存 JSON，不执行下载的 Python 代码；可传 `--proxy http://127.0.0.1:7890`。数据位于被 Git 忽略的 `data/deepseek-tokenizer.json`，程序启动时不访问下载站点。离线 tokenizer 加上 15% 及消息封装余量用于请求前估算；实际计费用量以接口返回的 `usage` 为准。文件缺失或不可读时退回更保守的 UTF-8 字节计数，接口会注明计数方式。

配置项可见 `.env.example`：`CHEMISTRY_CONTEXT_WINDOW_TOKENS`、`CHEMISTRY_CONTEXT_TRIGGER_RATIO`、`CHEMISTRY_CONTEXT_RECENT_TURNS`、`CHEMISTRY_CONTEXT_SUMMARY_TOKENS`、`CHEMISTRY_CONTEXT_TOOL_RESULT_TOKENS` 和 `CHEMISTRY_TOKENIZER_PATH`。

摘要失败时返回 `context_summary_failed`，不推进摘要覆盖范围，也不会丢弃历史。若最近保留轮次或当前资料本身已超过硬预算，会返回 `context_budget_exceeded`，应精简本轮资料、调小保留轮数或增大窗口。摘要已成功、后续回答失败时仍可复用摘要，因为它只覆盖此前已完成的历史。

`/chat` 响应和运行记录新增 `context`：包含是否压缩、摘要覆盖量、保留轮数、工具精简数量、压缩前后预计输入、输入预算、tokenizer 模式及摘要调用用量。摘要会产生额外模型调用，其 token 用量计入本轮总 `usage`，同时列在 `context.summary_usage` 中。旧数据库会自动增加摘要表和运行元数据列，原始记录保持不变。

## 配置和运行边界

- `.env.example` 包含全部配置；`.env`、虚拟环境、数据库均被 Git 忽略。Key 只在服务端使用，不写入业务数据库或响应。
- 默认使用 `deepseek-flash`，可通过 `DEEPSEEK_MODEL` 修改。默认关闭 thinking；打开 `DEEPSEEK_THINKING=true` 后会在内部保留并回传 DeepSeek 工具调用协议要求的 `reasoning_content`，公共接口不返回该字段。
- `CHEMISTRY_DATABASE_PATH` 默认为本目录下 `data/agent.sqlite3`；相对路径始终相对此目录解析。SQLite 保存已完成对话和每次运行的状态。
- 左侧固定的 **监控任务** 面板 可设置 Slurm 作业结束时的默认指令。在作业详情展开“作业结束时唤醒”，选择目标对话，也可为这次作业单独填写指令。已登记的专用指令和分组等待继续有效。
- 面板按对话筛选方案流程、Slurm 作业状态、持久等待及其唤醒投递。独立定时任务标明由用户或 Agent 创建。也可直接对 Agent 说“每 30 分钟检查这个对话的作业进度”，由它在当前对话登记、查看、修改、暂停或删除任务。
- 在同一页添加定时任务，选择对话、重复间隔和指令。首次唤醒在一个完整间隔后发生。平台宿主在线时执行，关闭浏览器后仍然有效。服务重启后将错过的周期合并为一次，同一任务仍在排队或运行时跳过该周期。暂停和删除只停止未来触发，已入队的回合可在原对话查看。
- `CHEMISTRY_SLURM_HISTORY_HOSTS=cluster-b` 为指定的已登记服务器启用作业看板历史留存。计算平台启动时回填 Slurm 可查记录，运行期间每 6 小时同步，遇到查询失败时 15 分钟后重试。作业记录按服务器、Slurm 账户、作业编号和提交时间去重，未成功覆盖的日期显示为不可查。
- `CHEMISTRY_SLURM_HISTORY_PATH` 可指定长期保存位置，默认使用平台数据目录中的 `slurm-history.sqlite3`。
- 模型每轮最多调用 6 轮工具，之后必须总结；单次模型请求默认超时 60 秒，整次运行默认超时 180 秒。错误和截断响应不会混入后续对话，不会自动重试计费请求。
- 以 token 预算替代旧的 120,000 JSON 字符限制；长历史会自动摘要。Token 用量累计已收到用量报告的模型响应及摘要请求；网络断开等情况下未收到的用量无法统计。
- 第一版使用单进程运行。同一会话同时发起两次任务时返回 409；不同会话可并发。重启后将上次未完成的运行标为 `interrupted`，已完成对话可继续。暂不支持运行中断点恢复或后台任务队列。
- 同一数据库有进程锁保护，重复启动服务会失败，不会清除正在运行的任务状态。失败任务保留此前已经执行完成的工具结果。
- 第一版提供独立前端与 HTTP 接口，尚未嵌入 Zotero 界面、自动读取 PDF 或联网检索；可分析主动传入的文献片段。分子工具提供计算描述符，不提供实验验证数据。
- 本地默认是单用户模式（`CHEMISTRY_AUTH_ENABLED=false`），继续只监听本机；远端 Compose 强制启用账号登录和 HTTPS Cookie。账号、会话、摘要及运行记录按所属用户隔离，不提供公开注册入口。

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
node --test tests/frontend.test.mjs
```

测试使用模拟 DeepSeek HTTP 响应，不需要 Key、不产生模型费用；覆盖真实 RDKit 计算、工具调用往返、连续对话、SQLite 重启、缺失 Key、上游认证/限流/超时、异常工具参数和轮数限制，以及静态资源、Markdown 安全渲染和前端历史消息重建。真实 DeepSeek 联调需要提供有效 Key 后单独进行。

配置 Key 并启动服务后，可另开终端执行真实联调（会调用 DeepSeek API）：

```powershell
.\.venv\Scripts\python.exe .\scripts\smoke_deepseek.py
```

联调依次验证连接、模型调用分子属性工具、带会话历史的分子相似度计算，以及运行记录与会话持久化。结果保存到被 Git 忽略的 `data/live-test-result.json`；脚本不读取或打印 API Key。可用 `--url` 指定服务地址。

真实上下文压缩联调可执行 `.\.venv\Scripts\python.exe .\scripts\smoke_context.py`。该脚本使用独立的合成测试数据库，以较小预算验证摘要、早期事实记忆、重启复用与原文保留，会实际调用 DeepSeek；不改变日常会话数据库或预算配置。报告保存在 `data/context-live-result.json`。

`constraints.txt` 固定本次在 Windows / Python 3.11 上验证的依赖版本；`pyproject.toml` 定义可接受的版本范围。

接口依据：[DeepSeek 入门](https://api-docs.deepseek.com/)、[工具调用](https://api-docs.deepseek.com/guides/tool_calls/)、[Thinking 模式](https://api-docs.deepseek.com/guides/thinking_mode/)、[LangGraph Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api)、[RDKit 文档](https://www.rdkit.org/docs/)。

## 通用计算平台

默认通过本机 SSH 配置连接服务器，由 Agent 写输入和提交脚本；平台登记作业、持久等待并在结束后恢复原对话。
Agent 本地生成的结构、图片和其他文件可直接在网页预览、下载或用于后续对话。
安装 `automation` 可选依赖不再安装 AiiDA。历史 AiiDA 数据保留在原目录；不迁移或重交旧作业。
已移除旧 AiiDA 执行层；无需安装引擎或初始化 profile。数据路径使用 `CHEMISTRY_PLATFORM_PATH`，现有历史路径和记录保持兼容。

配置、历史记录和数据路径说明见 [计算平台说明](docs/AUTOMATION.md)。共享账号服务不会开放本地集群执行工具。

[远程终端、联网搜索、流式对话与重启恢复](docs/agent-runtime.md) · [自动化计算](docs/AUTOMATION.md)

## 界面开发

新增或修改界面请先阅读 [统一界面设计规范](docs/design/ui-spec.md)，复用 Solid Glass 语义 token、Lucide 图标和公共交互组件；Harness 别名仅用于兼容。验收包含实际浏览器的宽窄布局与键盘交互。

UI 阶段进度、分支关系及统一基线的合并方案见 [UI 整合进度](docs/design/ui-integration-status.md)。新功能应从已包含本轮 UI 的 main 创建分支；仍在维护的旧分支先同步该 main，再按统一规范开发。
