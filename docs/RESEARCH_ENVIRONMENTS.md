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

这些接口适用于当前单用户部署。远端网页通过 SSH 隧道访问，沿用 localhost 管理边界；没有开放无认证公网控制接口。

## 部署方式

### 本机试用

沿用已有服务。设置按用户任务分为三组：

- 通用：常规、外观。
- 智能体与知识：Agent 与模型、知识与技能。
- 计算与运行环境：运行概览、服务器与环境。

“服务器与环境”只有一张节点列表；每个节点选择同机、SSH 或 HTTP Runner。编辑原节点可切换连接方式，保留节点 ID 和现有作业引用。SSH 候选来自平台宿主的配置，不是浏览器所在电脑的配置。旧 SSH 授权按原 ID 显示；显式停用或移除的节点不会回退到旧 SSH。移除只保留禁用标记，不删服务器文件或取消作业；停用期间该节点的工具访问和监控查询暂停。节点列表保持稳定顺序。

“Agent 与模型”直接显示托管 Agent 的宿主。三种 Agent 按各自卡片保存；保存一个不会提交另一张卡片的草稿。点击“新对话默认”会先保存该卡片，成功后更新默认标记，不改变已有会话的后端。Chem Agent 的模型凭据与默认后端属于两个服务，第二步失败会明确报告，不能宣称一起原子保存。

“运行概览”读取实际宿主名和监控任务状态，展示启用节点数与知识配置入口。关闭浏览器不影响宿主上的服务；是否会随个人电脑关机中断，取决于该宿主是否就是个人电脑。不能根据 localhost 地址或 SSH 隧道推断部署位置。程序已安装、配置已保存和上次连接检测是不同状态。

### 本地关机仍要自动推进

在允许常驻服务的服务器/管理机运行完整平台：网页后端、独立监控循环、Agent 原生进程都在该机器。运行 `scripts/run_research_platform.py`，顶部只配置数据目录、监听地址和端口；重用现有 Python 项目环境即可。

浏览器通过 SSH 本地端口转发连接这一个平台实例，例如：

```sh
ssh -N -L 8766:127.0.0.1:8766 cluster-b
```

平台数据只有一个权威副本。迁移现有平台前停止旧服务，完整备份数据库及平台/工作空间目录，保持 session/project/job ID 与路径引用，不让两台主机同时修改同一个 SQLite。不要用 NFS 共享活动 SQLite 文件。

Agent 运行位置目前跟随平台宿主，设置页明确显示该位置。此版本没有把某个原生 Agent 单独迁移到另一台 Runner 的进程代理；选择计算 Runner 不等于搬迁 Agent。平台整体放在 cluster-b 即可在服务器运行 Agent，同时调度 cluster-a/cluster-b。

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
