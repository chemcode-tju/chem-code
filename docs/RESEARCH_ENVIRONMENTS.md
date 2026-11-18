# 多服务器环境、OpenViking 与共享 Skill 插件

开发分支 `codex/research-environments`，基于 `origin/main` 的 `50964ef`。

## 已实现的边界

- 一个研究会话/方案中的每个 job 保留自己的 host；CPU 和 GPU 作业可以并行，平台不增加并发上限。
- 执行环境支持原有 SSH、新增同机执行以及认证 HTTP Runner。现有 host 别名可以改接 Runner；不改变作业 ID、原目录和 Slurm 提交回执。切换时必须连接同一账号/文件系统，不能把已有别名指向另一个集群。
- Slurm 提交仍使用已有的持久 reservation 与作业名对账。Runner 不另外创建提交队列；恢复平台不会重放已提交的任务。
- 通用 shell 命令带请求 UUID。Runner 先落盘 intent，再执行；完成回执可重读。失联/进程中断留下 unknown，不能猜测失败并自动重放。Agent 通过 shell、文件及调度器核对后决定下一步。
- 文件引用继承上游依赖；跨服务器由平台传输，记录来源与 SHA-256，并在下游提交前校验。当前跨机器大文件经过平台宿主，尚未实现服务器间直传或对象存储优化。
- OpenViking 是独立知识服务。研究记忆的结构化来源记录仍是平台事实源；OpenViking 提供独立笔记与语义检索，并接收可重建的来源投影。没有上传原生聊天、密钥或全部计算输出。
- Skill 插件由平台统一安装/启停，支持标准 SKILL.md 目录和 Anthropic `.claude-plugin/marketplace.json` 的本地 `skills` 列表。官方来源提供 Anthropic、OpenAI。每次安装固定实际 commit/内容哈希，保留旧版本及许可证，不运行安装钩子。
- Chem Agent、Codex、Antigravity 获取相同的知识/Skill 工具。外部平台可通过 MCP 访问相同项目，不需要共享模型原生线程。
- Codex/Antigravity 实例可以运行在平台本机或已启用的 SSH 主机；每个对话独立保存主机、可选文件夹、Agent/模型和监控位置。计算作业仍可发往另一个 CPU/GPU 节点。

这些接口适用于当前单用户部署。远端网页通过 SSH 隧道访问，沿用 localhost 管理边界；没有开放无认证公网控制接口。

## 部署方式

### 本机试用

沿用已有服务。设置按用户任务分为三组：

- 通用：常规、外观。
- 智能体与知识：Agent 与模型、知识与技能。
- 计算与运行环境：运行概览、服务器与环境。

“服务器与环境”只有一张节点列表；每个节点选择同机、SSH 或 HTTP Runner。编辑原节点可切换连接方式，保留节点 ID 和现有作业引用。SSH 候选来自平台宿主的配置，不是浏览器所在电脑的配置。旧 SSH 授权按原 ID 显示；显式停用或移除的节点不会回退到旧 SSH。移除只保留禁用标记，不删服务器文件或取消作业；停用期间该节点的工具访问和监控查询暂停。节点列表保持稳定顺序。

“Agent 与模型”先选择主机，再检测已安装的 Codex/Antigravity 或手动添加实例、路径和模型。同一主机可有多个实例。Chem Agent 使用平台模型凭据，随平台运行。保存一张卡片不会提交其他草稿。研究空间设置、管理接口和默认空间逻辑已移除；已有对话的目录与会话绑定继续保留。

输入框上方依次选择服务器、可选文件夹和作业监控，下方选择当前服务器上的 Agent、模型及推理强度。服务器列表中“此计算机”指平台所在机器。切换服务器清空不适用的目录；同机换 Agent 保留目录，监控位置始终独立。

文件夹支持本机/SSH 目录浏览及手动路径，不选时自动创建独立会话目录。显式选择的目录就是 Agent 工作目录，不再附加会话 ID。同一文件夹中的各会话历史使用独立的 `.chem-code/history-<会话ID>.json`，避免覆盖彼此或用户已有的 `platform-history.json`。浏览目录不会写文件，新目录在启动对话时创建。旧版空间绑定只用于恢复旧对话，不再成为新对话默认项。

“运行概览”读取实际宿主名和监控任务状态，展示启用节点数与知识配置入口。关闭浏览器不影响宿主上的服务；是否会随个人电脑关机中断，取决于该宿主是否就是个人电脑。不能根据 localhost 地址或 SSH 隧道推断部署位置。程序已安装、配置已保存和上次连接检测是不同状态。

### 本地关机仍要自动推进

在允许常驻服务的服务器/管理机运行完整平台：网页后端、独立监控循环、Agent 原生进程都在该机器。运行 `scripts/run_research_platform.py`，顶部只配置数据目录、监听地址和端口；重用现有 Python 项目环境即可。

浏览器通过 SSH 本地端口转发连接这一个平台实例，例如：

```sh
ssh -N -L 8766:127.0.0.1:8766 cluster-b
```

平台数据只有一个权威副本。迁移现有平台前停止旧服务，完整备份数据库及平台/工作空间目录，保持 session/project/job ID 与路径引用，不让两台主机同时修改同一个 SQLite。不要用 NFS 共享活动 SQLite 文件。

不必为远端 Agent 搬迁整个平台：设置中可直接选择 SSH 主机上的 Codex/Antigravity。HTTP Runner 仍是计算命令通道，不是原生 Agent 进程代理。完整平台放在常驻机时，可继续把 Agent 和计算分别放到其他主机。

### 原生 Agent 与监控的独立位置

远端主机需要 Python 3、可用的 SSH 认证，以及已经安装并登录的原生 Agent；检测不会替用户安装或登录。Antigravity 的平台工具桥接需要 SSH 允许 Unix socket 转发。使用平台已有主机指纹/SSH 配置，不关闭主机校验、不转发本机认证 Agent。原生 Agent 自身工具权限及计算并行沿用会话设置。

一次对话使用一条 SSH 连接承载原生协议、工具桥接和文件同步。远端原生 home 位于 `~/.local/state/chem-code/<平台标识>/agents/<实例>/<会话>`，共享该主机的认证/技能，但不读写桌面端原生会话。远端内核锁保证同一会话只有一个写入者；恢复、模型枚举和手动压缩均使用选定主机。消息投递账本先检查回执，再打开原生连接；结果不明的消息不会盲目重发。

远端生成文件按现有发布规则取得不可变本机副本，持久保存真实主机与路径，刷新/重启后链接仍可预览下载。Skill 脚本复制到当前 Agent 所在研究目录，保留 Agent 已修改的工作副本。

监控可选“自动”、平台本机或 SSH 主机，与 Agent 分开。远端观察器只查询队列并落盘结果，不调用模型或提交作业；平台断线时继续观察，重连后由原等待/事件账本消费。切换监控时先验证新位置能访问队列，再持久保存交接代数、停用旧观察器、启用新观察器；中断后续接同一次交接。失联不会触发重复提交。

跨服务器监控需要监控主机可以 SSH 到作业主机；预检失败保留旧观察器并显示原因。HTTP Runner 作业目前由平台本机监控。远端重启后由在线平台重新启用观察器，未安装额外系统启动项。

**远端观察器不等于完整的远端会话调度器。** 本机平台关机后，远端队列观察仍可记录结果，但自动唤醒 AI、工具调用和推进后续步骤要等平台恢复。若这些也必须连续进行，完整平台应部署在常驻机。关闭浏览器本身不会中断平台。

### 各集群的 Runner

在集群允许的登录/管理主机运行 `scripts/run_execution_runner.py`。首次运行生成权限为 0600 的 `~/.local/state/chem-runner/connection-token`，不将内容写日志。设置页添加环境，填写地址、令牌及 CPU/GPU 等描述标签。

Runner 默认只监听 loopback。跨主机可以用长期 SSH 隧道（由系统服务保持），或部署在受控网络中经 HTTPS 访问。令牌允许该 Unix 账号的终端操作，应留在服务器配置中，不放入模型提示词。

每个目标只需一条常驻连接/服务路径；工具通过 HTTP 请求目标 Runner，不再每次启动 SSH。实际重计算仍交 Slurm。Runner 本身不调用模型，不限制已就绪作业数量。更换服务地址时不会把旧连接令牌自动发送到新地址，需要重新填写对应凭据。

Linux 可将上面两个脚本分别放入 systemd user service，使用安装目录中的 Python 绝对路径：

```ini
[Unit]
Description=Chem Code research platform
After=network-online.target

[Service]
WorkingDirectory=%h/Chem-Agent
ExecStart=%h/Chem-Agent/.venv/bin/python %h/Chem-Agent/scripts/run_research_platform.py
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
```

Python 路径按实际 conda/项目环境调整；Runner 换成对应脚本。退出登录后是否保持 user service 取决于站点的 linger 配置。macOS 可沿用项目现有 launchd 部署方式。本次开发未远程安装服务或改动生产启动项。

## OpenViking 接入

实现针对官方 HTTP API，核对版本：`volcengine/OpenViking@a4b543db16018f28276dd36049fc5114450329bd`。

1. 按上游部署文档启动 OpenViking，并配置其 embedding/模型及账号授权。
2. 在“知识与技能”填写 endpoint、API Key，启用后保存、检测连接。
3. 默认根为 `viking://~/resources/chem-code`，平台在下面按 owner/project 稳定 ID 分区。
4. “同步科研记忆”手动推进一次，后台每 60 秒检查尚未投影的记录。同步过程不调用聊天模型；OpenViking 索引可能使用其配置的模型。
5. 连接失败保留平台原记录；重启后按内容哈希补齐。相同内容不重复索引，忘记记录会删除投影。跨系统没有分布式事务；外部 OpenViking 在离线期间可能保留旧投影，平台 read 始终核验当前记录。

`knowledge_search/read/write` 访问知识；查询默认包含当前项目和个人区，写入可以用 `scope="personal"` 保存跨项目偏好。`memory_*` 访问有版本、参数和来源的结构化记忆。`research/` 是同步记录保留目录，Agent 修改它必须走 `memory_save`。研究事实与偏好不应来自未经确认的模型推断。

部署 OpenViking 本体、配置 embedding、真实语义召回质量与实际多机器网络验收需在目标环境验证；当前测试使用符合所核对 HTTP 协议的测试服务，不假称已连接真实 OpenViking。

## Skill 插件与外部 Agent

设置中添加仓库 URL/本地目录；可选择插件名（如 Anthropic `document-skills`）和 commit/tag。OpenAI 仓库没有该市场清单时留空插件名，扫描其标准技能目录。只展示元数据，全文和附件通过 `skill_read` 按需读取；`skill_materialize` 将脚本与引用资料复制到当前工作空间，Agent 可以正常修改与运行，不强制特定科学方案。

这是技能包接入，不是任意厂商插件运行时：MCP 插件、IDE 扩展、宿主特有 hooks 不会被自动执行。技能依赖由 Agent 在目标运行环境确认，不能把安装 SKILL.md 当作相关科学软件已经安装。

外部 MCP 客户端连接：

在“知识与技能 → 外部 Agent 接入（MCP）”选择个人知识或项目，一键复制 HTTP 地址或 stdio 配置。HTTP 地址使用当前浏览器访问地址，保留 SSH 隧道的本地端口；stdio 片段使用平台主机的 Python、模块路径和本机服务端口，明确标为在平台主机运行。远端平台的绝对程序路径不能直接当作用户 Mac 的程序路径。

```text
http://127.0.0.1:8766/api/knowledge/mcp?project_id=<项目UUID>
```

不填 project_id 为个人研究区。支持 Streamable HTTP 的 JSON 响应模式；旧 stdio 客户端使用项目环境 Python 运行 `-m chemistry_agent.knowledge.mcp`，通过 `CHEM_KNOWLEDGE_URL` 设置地址。远端使用同一个 SSH 隧道。身份由平台决定，不接受模型指定 owner_id；无须另造聊天会话。

stdio 代理直接连接指定平台，不继承系统 HTTP 代理，以免 loopback 隧道被转发到无关代理。选择项目控制默认检索/写入范围；同一账户仍可通过记忆 ID 复用本人其他项目的事实，不把项目选择伪装成新增权限隔离。

## 来源与设计取舍

- [T3 Code remote environments](https://github.com/pingdotgg/t3code/blob/main/docs/user/remote-access.md)：参考环境管理，不复制其工作区与主机绑定到研究会话。
- [HAPI architecture](https://hapi.run/docs/guide/how-it-works)：参考 Hub/Runner/客户端分工；本项目保留自己的 Slurm 账本及等待语义。
- [OpenViking HTTP API](https://github.com/volcengine/OpenViking/blob/a4b543db16018f28276dd36049fc5114450329bd/docs/en/api/01-overview.md)：通过 HTTP 集成，没有 vendoring 上游实现。
- [Anthropic Skills marketplace](https://github.com/anthropics/skills/blob/main/.claude-plugin/marketplace.json)、[Agent Skills](https://agentskills.io/specification)、[OpenAI Skills](https://github.com/openai/skills)：兼容数据格式，安装时保留来源版本和许可证。

没有恢复 AiiDA，也没有为研究计算引入 Git 工作树。插件源码缓存的 Git 仅用于下载和固定版本。

## 底座验收（2026-11-17）

- Python 完整回归：412 passed，2 skipped；前端 Node 测试：101 passed。后续凭据切换与插件版本修复再次通过相关 28 项回归。
- 同一方案的两个执行环境：使用本地模拟 Slurm，CPU 步骤产生二进制文件，GPU 步骤读取已校验文件。重建管理器并重复推进后仍只有两个作业。此项不代表已经连接真实 cluster-a/cluster-b。
- Runner：另外启动真实 loopback HTTP 服务，验证认证、重建平台连接、重读持久回执；相同工具请求只产生一次文件副作用，未提交集群作业。
- 知识：使用 OpenViking HTTP 契约测试验证项目/个人范围、别名 URI、来源同步、断线重试和忘记记录；没有真实 embedding 召回验收。
- 插件：在独立预览数据目录安装并读取 Anthropic `document-skills`（commit 前缀 `e4bb5d960d87`）；启停和版本保留验证通过。没有运行其中脚本，也没有把下载包加入仓库。
- 页面：1440、768、390 px，明暗模式和中英文；实测插件展开、启停、环境编辑以及本机模式隐藏远程凭据。截图保留在开发机 `/private/tmp/chem-research-knowledge-settings.jpg`。
- `ruff check` 和 `git diff --check` 通过。生产 8766 服务、SSH 配置、现有任务和数据没有改动。

## 设置整合复验（2026-11-17）

- 完整 Python 回归：417 passed，2 skipped；前端 Node 回归：102 passed。`ruff check`、`git diff --check` 通过。
- 新增回归覆盖旧 SSH → Runner 的身份保留、重名拒绝、停用/移除不回退、稳定排序、SSH 别名映射，以及并发保存两个 Agent 卡片时不互相覆盖、不改变已有会话。
- MCP 按项目检索/写入验证通过；生成的 stdio 配置在独立进程实际读到 8 个共享工具。测试故意设置不可达系统代理，loopback MCP 仍正常，未调用模型。
- 在独立的 8857 预览服务实测节点新增、同 ID 切换连接方式、检测成功/失败、停用清除历史状态、移除后不恢复旧 SSH。测试只使用本机和虚构 SSH/Runner 配置，没有连接 cluster-a/cluster-b。
- 实测保存 Antigravity 后 Codex 草稿仍未提交，选择默认时保存当前卡片，刷新保留默认值；Chem Agent 恢复默认正常。MCP 项目选择与复制地址正常。
- 1440/768/390 px、浅色/深色、中英文检查通过；窄屏内容无横向溢出，代码仅在自身区域滚动，操作后保留键盘焦点。截图：`/private/tmp/chem-settings-runtime-final.jpg`、`/private/tmp/chem-settings-nodes-final.jpg`、`/private/tmp/chem-settings-mcp-final.jpg`。
- 原 8766 服务运行在另一个工作树；本次未重启它、修改其配置或提交计算。预览不代表 OpenViking 或远程 Runner 已真实部署。

## 主机、Agent 与监控位置验收（2026-11-17）

以下为首版验收；其中研究空间设置已由下节的对话内位置选择替代。

- 完整 Python 回归：432 passed、1 skipped；前端 Node 回归：102 passed。Ruff、JavaScript 语法检查与 diff 空白检查通过。
- 实际 loopback SSH 协议连接配合测试 Codex/Antigravity 进程，验证模型读取、两轮会话恢复、Codex 原生压缩、Antigravity 经 SSH Unix socket 调用平台工具。不是实际远端模型推理或 cluster-a/cluster-b 联调。
- 验证远端写入锁、已完成/结果不明消息在连接前去重、文件下载与真实来源跨重启保留、远端 Skill 工作副本不被覆盖。修改研究空间默认目录后，已有对话只切换监控不会搬迁目录或重建原生会话。
- 观察器以独立进程读取模拟队列，控制进程结束后仍记录完成；重复启动只有一个内核锁持有者。交接失败保留旧观察器，丢失启用响应后按同一代数恢复。全过程没有 sbatch。
- 真实浏览器验证检测与手动添加多个实例、研究空间默认、主机→Agent→模型菜单、独立监控选择、390 px 窄屏及键盘返回/焦点、明暗模式；无浏览器错误。截图 `/private/tmp/chem-host-agent-settings.jpg`、`/private/tmp/chem-host-agent-model-menu.jpg`。
- 开发预览使用独立 8857 端口和临时测试数据库；未改动生产 8766、SSH 配置、已提交作业或远端启动项。

## 对话内位置选择验收（2026-11-17）

- 输入框上方统一为服务器、可选文件夹、作业监控；下方保留权限与 Agent/模型/推理强度菜单。设置页移除研究空间导航、表单、默认模板及其保存接口。
- 新对话单独选择位置；选择文件夹后直接在该目录运行，不再追加会话 ID。不设置文件夹时使用平台自动目录。切换同主机 Agent、模型或监控保留目录；更换主机清空旧主机目录。
- 多个对话共用目录时，各自保存独立的 `.chem-code/history-<session>.json`；旧对话沿用已有目录快照，不搬迁文件、不覆盖用户的 `platform-history.json`。
- 完整 Python 回归：433 passed、1 skipped；前端 Node 回归：104 passed。包含实际 loopback SSH 的目录绑定、恢复、原生压缩及重复事件去重；没有调用真实模型或提交集群计算。
- 独立 8857 浏览器验收：服务器选择、远程目录浏览/手动路径、同主机 Agent 切换、模型与独立监控、设置关闭后草稿保留；设置页无研究空间入口。窄屏保持可用，模型控件空间不足时换行。
- 预览使用测试主机与测试模型；生产 8766 服务、SSH 配置与研究数据未改动。

## 正式服务接入（2026-11-17，用户随后授权）

- `com.chem-code.automation` 已切换到当前 `fffe/Chem-Agent/src`，仍监听 `127.0.0.1:8766`。保留旧工作树的 Python 环境、`.env`、数据库及平台数据目录，不覆盖旧工作树未提交的代码。
- 启动器位于 `~/.local/share/chem-code/service/start.py`；原 launchd 配置与 SQLite 一致性备份保存在同目录的 `backup-*` 子目录，权限收紧到当前用户。
- 原进程退出期间第一次 bootstrap 失败；待退出完成后重新加载成功。健康检查与主机接口通过，实际浏览器可读取旧对话及新版位置控件；未发送模型请求或提交计算。登录自启、异常重启继续启用。

## 共享目录文件归属修复（2026-11-17）

- 环境检查回合错误将共享目录的 523 个历史文件关联为输出，并复制约 652 MB。根因是回合结束时递归发现全部文件、同步空结果后又扫描本地镜像，且未建立回合前基线。
- 现改为回合前后有界元数据差异发现；不完整基线不推断新文件。远端仅传输指定变化路径，空列表不扫描共享目录或镜像。明确发布支持旧文件复用，列表称为“相关文件”。元数据变化只能证明观察期间发生变化，不能证明共享目录中唯一写入者是 Agent。
- 错误关联进入可恢复排除表，保留原文件、附件及修复清单；浏览器和正式 API 均确认原对话不再展示这批文件。相关后端 20 项、前端 104 项回归通过，未调用模型或提交计算。
- 官方参考：`openai/codex` 的 `core/src/turn_diff_tracker.rs` 依据已应用补丁跟踪每轮差异，`app-server-protocol` 暴露 `fileChange` 与 `turn/diff/updated`。后续应优先消费原生变更事件；Python/ASE 和计算程序输出仍需平台自己的产物登记，不能把补丁事件视为所有文件写入的完整记录。

### Codex 原生文件事件接入

- 已消费 `item/completed` 中成功的 `fileChange`，按 `add/update` 的路径发布当前会话成果，重命名使用 `move_path`。失败、删除及其他会话/回合事件不新增下载项；不从统一 diff 文本猜测路径。
- 文件 ID、变化路径及预览失败原因保存在工具结果与检查点。文件按路径及内容哈希去重，重复原生事件合并工具记录；预览失败不会把成功编辑改判为需要重跑的失败回合。
- Python/ASE、终端与计算程序的输出继续使用有界元数据差异和明确文件发布。原生事件不依赖目录扫描成功；远端测试故意使扫描不可用，仍完成文件登记、下载和跨重启来源恢复。
- 相关回归 21 项通过，包含新建、修改、重命名、失败、删除、重复事件、缺失文件以及其他回合/会话过滤；SSH 使用真实 loopback 协议和测试 Codex 进程，不代表调用了真实模型。
