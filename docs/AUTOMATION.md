# AiiDA 自动化任务与聊天工作流

目标：在网页选择服务器、提供一个结构，预览 AiiDA 生成的输入与调度脚本，明确提交后查看真实任务状态。

当前面向本地单用户。状态监控与工作流推进不调用模型；新发生的任务故障可唤起已配置的模型诊断，产生相应模型用量。实际重提交由明确授权的恢复策略约束。

## 本地计算设置

### macOS 常驻服务

在已完成 `.conda` 自动化环境配置的运行目录执行 `.conda/bin/python scripts/install_automation_service.py`，安装当前用户的 `com.chem-code.automation` LaunchAgent。它使用该目录现有设置、数据库和绝对 Python 路径，监听 `127.0.0.1:8766`；登录后启动，进程退出后由 launchd 重新拉起。安装器发现端口被手动启动的服务占用时会停止，避免两个实例争抢数据库；请先等待聊天任务空闲，再关闭旧实例并安装。

日志为运行目录的 `data/automation-service.log` 和 `data/automation-service-error.log`。检查状态：`launchctl print gui/$(id -u)/com.chem-code.automation`。需要停止服务时使用 `launchctl bootout gui/$(id -u)/com.chem-code.automation`；重新启动使用 `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.chem-code.automation.plist`。退出登录或电脑休眠期间，本机服务不能保持访问；此托管不改变计算权限或公网入口。

在启用自动化的单用户服务中，设置页通过以下接口调用同一个 Python 后台。首次读取即使还没有 profile 也会返回缺少的项目。

| 接口（均位于 `/api/automation`） | 用途 |
| --- | --- |
| `GET /settings`，可附 `?server_id=UUID` | 返回本机 AiiDA 和 daemon 状态、服务器、软件、赝势目录、执行目标、旧 protocol、缺项与默认 ID |
| `POST /settings/initialize` | 创建独立 SQLite/ZeroMQ profile，按需启动 daemon，重复调用保留现有数据 |
| `POST /codes` | 登记 `server_id, name, version, executable, environment_script`。只有 VASP 对接计算适配器，其他程序可登记和回显 |
| `POST /settings/potential-sources` | 登记服务器 ID 和 PBE 库目录，准备计算时才按元素导入 |
| `GET /settings/servers/{server_id}/queues` | 查询服务器的 Slurm 或 PBS 队列 |
| `POST /settings/execution-targets` | 保存目标 ID、服务器、VASP code、赝势源、队列和资源，可设默认和配额回退 |

默认目标只保存运行环境。Agent 根据用户这次的任务给出 INCAR 与 k 点网格，单任务预览和多步方案使用同一选择规则。显式 ID 优先，其次是默认执行目标、默认旧 protocol、唯一执行目标。旧 protocol 可继续提供原有参数。方案保存后，计算使用已记录的配置快照。

聊天可以直接说“帮我注册软件 VASP 6.4，路径……”，或“在 cluster-b 上把赝势目录设为……”。计算设置写权限只限本轮用户的明确请求，也支持首行 `/configure`。设置回显和队列查询可直接读取。

## 当前能力与边界

| 功能 | 实现及验证范围 |
| --- | --- |
| 服务器 | OpenSSH 别名、本机密钥或 ssh-agent、macOS 钥匙串密码；首次探测显示 SHA256 指纹 |
| 连接检查 | AiiDA SSH、调度器查询、工作目录现有上级目录的写权限；不提交或创建远端文件 |
| 调度器 | Slurm、PBSPro/OpenPBS、Torque 的 AiiDA 插件；三种脚本均有真实 dry-run 测试 |
| 结构 | POSCAR、CIF、EXTXYZ；单帧、完整周期晶胞、最多 10,000 原子 |
| 输入保护 | 保留可映射到 VASP 的固定原子和方向约束；拒绝非有限坐标或无法保真的约束，初始磁矩/电荷需显式转为计算参数 |
| 联通测试 | 自带 CalcJob：上传 POSCAR，在计算节点记录 hostname 和 SHA256，取回并校验；不做科学计算 |
| VASP | aiida-vasp CalcJob；显式 INCAR、k 点、赝势族及映射；远端 Code 和环境脚本注册 |
| GPU | Slurm 的 GPU 数量及可选类型；PBS GPU 语法需按具体站点适配 |
| 预览 | AiiDA 原生 dry-run；显示 POSCAR、INCAR、KPOINTS、提交脚本；不向网页返回 POTCAR |
| 提交 | 固定预览 ID；持久化预留记录；重复请求返回同一 AiiDA 节点 |
| 状态 | AiiDA process 状态、scheduler 状态、作业号、退出码、更新时间及输出；最近 100 个任务 |

Agent 可创建包含并行分支的计算方案。用户可在原会话查看任务事件、停止方案，并根据恢复策略跟进失败节点。调度器接单、作业结束、输出取回和科学收敛在不同阶段判定。

## 安装和启动

创建独立环境，然后安装本地包及自动化依赖：

```bash
conda create -n chem-agent python=3.12 pip
conda activate chem-agent
python -m pip install -e '.[automation,dev]'
python scripts/start_automation.py
```

这里的 pip 命令用于安装本地项目的可选依赖组；Python 环境由 Conda 管理。不要直接套用旧的 Windows `constraints.txt` 锁定自动化环境。

打开[计算设置](http://127.0.0.1:8766/settings#automation)，按页面提示初始化本地 AiiDA 环境并检查 daemon。已有安装也可用 `scripts/setup_automation.py`。升级已有环境后需重新安装本地包，让 AiiDA 发现 `chemistry.ssh` 传输插件。已有 daemon 请在空闲时重启以刷新插件目录。

默认的 `AIIDA_PATH` 位于项目 `data/aiida`，profile 名为 `chem-agent`。可用 `CHEMISTRY_AIIDA_PATH` 和 `CHEMISTRY_AIIDA_PROFILE` 指定；若改 profile 名，需同步修改初始化脚本顶部。该路径不使用或覆盖用户的默认 AiiDA 配置。

当前采用 AiiDA 2.9 的 SQLite + ZeroMQ 来验证流程。大量并发计算应迁移到常驻管理机器，使用 PostgreSQL 及合适的 broker，并实现批次限流。参见 [AiiDA 安装能力说明](https://aiida.readthedocs.io/projects/aiida-core/en/stable/installation/guide_quick.html)。

## 第一次提交

1. 在设置的模型页选择模型并完成登录。计算设置会显示本地执行环境、服务器、软件、赝势和队列的准备情况。
2. 选择服务器登录方式。已有 SSH 别名直接沿用配置。直接连接填写主机、端口、用户名，选择钥匙串密码或本机私钥路径。私钥留在服务所在机器，服务器端安装对应公钥；有口令的私钥可先载入 ssh-agent。
3. 连接前显示服务器主机密钥的 SHA256 指纹。与管理员提供的指纹核对后再信任并保存；已有 known_hosts 条目也会被检查。密码只在设置的专用输入框中输入。
4. 测试 SSH、调度器查询和工作目录权限。选择站点有效的队列，登记软件名、版本、可执行文件绝对路径，以及需要时加载的环境脚本。为 VASP 关联服务器上有权使用的赝势根目录。
5. 在对话里描述结构、计算方法、INCAR 和 k 点需求。让 Agent 生成预览，检查输入与资源，再确认提交。先出现 AiiDA ID，调度器接收后才有实际作业号。

预览里的 `aiida-None` 是未存储 dry-run 节点的占位作业名称，真实提交后由 AiiDA 节点编号替换。预览不等于提交。

## VASP 配置

注册 Code 时提供真正的 `vasp_std` 绝对路径及可选环境脚本；注册只检查文件存在，不执行 VASP。环境脚本会通过 `source` 写入计算节点的提交脚本。MPI 默认使用 `mpirun -np {tot_num_mpiprocs}`，需要与站点环境匹配。

在设置里填写获授权的远端赝势库根目录。计算准备阶段按元素选取该目录下的 POTCAR，导入到所需的本地 AiiDA family。已有 family 和映射继续可用。

INCAR 必须显式填写，程序不会替你选择泛函、色散、ENCUT、ISPIN、MAGMOM 或收敛阈值。单点应明确设置 NSW=0 等参数；结构优化需要匹配的 IBRION、NSW、ISIF、EDIFFG。GPU 数量、GPU 类型、MPI 进程数也由用户按程序构建和站点要求指定。

单任务提交 CalcJob；优化后单点使用本项目的 `chemistry.relax_static` WorkChain。恢复执行器创建独立的新任务，保留失败记录；不包含 aiida-vasp VaspWorkChain 的检查点重启策略。VASP 返回正常仍需独立核对电子/离子收敛和研究所需的科学标准。

## 实现结构

- `automation/models.py`：输入和资源的严格校验。
- `automation/routes.py`：本地 API；拒绝跨源、非 loopback 和共享账号模式。
- `automation/service.py`：串行启动隔离 Python 工作进程，避免 AiiDA 的全局 profile/阻塞 I/O 影响聊天事件循环。
- `automation/worker.py`：受限 JSON 操作分派与日志隔离。
- `automation/backend.py`：Computer/Code、dry-run、提交、查询和持久化防重复记录。
- `automation/probe.py`：真正的 AiiDA CalcJob 与结果 Parser。
- `web/settings.html`：计算设置入口。旧 `/automation` 链接会跳转到该页。

短请求交给工作进程，已提交的作业由 AiiDA daemon 管理。HTTP 超时不应让客户端新建相同任务：使用同一预览重试。预留记录存在但找不到 AiiDA 节点时返回 `submission_uncertain`，阻止盲目重复提交，需要人工核对。预览后服务器、MPI 或 Code/赝势身份变化时要求重新预览。

关闭浏览器或聊天不影响 daemon。关闭管理机器会暂停监控、取回和后续流程，已经提交的集群任务通常继续。备份需包含完整 `data/aiida`（AiiDA 数据、仓库及提交日志）；原聊天数据库的备份工具不包含这一部分。

## 验证

```bash
python -m pytest
python -m ruff check src/chemistry_agent/automation scripts/setup_automation.py scripts/start_automation.py tests/automation
node --test tests/frontend.test.mjs
```

自动化测试使用临时 AiiDA profile，不访问真实集群。覆盖三种调度器 dry-run、结构验证、预览不提交、同一预览幂等、提交结果不明时停止重试，以及 loopback/Origin 限制。真实集群测试报告保留在本地忽略的 `data/`；PBS 仍需连接实际站点验证。

## 在聊天中建模、预览和提交

启动 `scripts/start_automation.py` 后访问 `/`。计算工具只在开启自动化的本地单用户模式注册；共享账号模式不会获得集群工具。模型入口位于 `/settings`，支持 ChatGPT 账号授权、DeepSeek API Key 和 OpenAI 兼容接口；保存后下一次请求生效。旧 `DEEPSEEK_API_KEY` 可继续作为默认配置。模型凭据不进入计算文件或建模环境，ChatGPT 授权也不会开启共享账号模式。

1. 点击 **添加结构文件**，上传单帧 POSCAR、CIF 或 EXTXYZ；也可以在对话中粘贴结构，让 Agent 调用 `import_structure`。
2. 需要建模时，让 Agent 编写 ASE Python：参数放文件顶部，使用 `if __name__ == "__main__":`，输出 `structure.extxyz`。修改已有结构时传 `input_structure_id`，代码从 `input.extxyz` 读取。代码与产物分别保存，可以下载复用。
3. Agent 先用 `calculation_setup` 获取真实服务器、Code、环境与赝势映射，再用 `calculation_preview` 生成实际输入。结构通过 `structure_id` 引用，无需反复把坐标发给模型。
4. 在聊天下方检查实际 POSCAR、INCAR、KPOINTS、提交脚本及资源，点击 **确认以上参数并提交计算**。此操作独立于模型调用，也不需要模型批准。文字回复“确认”会引导用户点击对应卡片。
5. 可以继续问“查询 AiiDA #29 的状态和结果”，或在已提交卡片中点击刷新。任务与对话运行是不同记录；对话结束不会停止 AiiDA 作业。

提交确认绑定当前会话与具体预览 ID。模型没有提交工具；另一会话的预览无法确认。同一预览重复确认复用已有作业。服务器/Code 配置变化会使预览失效，需要重新生成。提交失败或网络结果不明时保留预览和提交记录，不通过新建预览盲目重试。

结构、代码与预览保存于独立的会话产物目录，聊天压缩不会删除它们。`calculation_artifacts` 可以恢复引用。`calculation_job` 只查询本模块管理的 AiiDA UUID/PK，返回状态、解析结果、取回文件名与限长诊断日志；不会返回 POTCAR 内容。正常退出、电子收敛和科学精度收敛仍分别判断。

### 建模执行器的范围

当前内置执行器针对 macOS：使用系统 `sandbox-exec`，仅允许读取 Python 环境与必要系统库，工作目录可写，禁止网络、用户私有文件与外部子进程。子进程不继承 API Key、SSH agent 等环境变量；限 20 秒 CPU、30 秒墙钟、单文件 2 MB。无可用沙箱时拒绝执行，保留代码供下载，不回退到无隔离 Python。尚未提供容器执行器或跨平台的硬内存限额，不适合大模型推理、优化器或长期模拟。

上传与建模结果仍受首版结构校验约束：1–10000 原子，完整周期晶胞，有限坐标；约束、初始磁矩或电荷不会被静默丢弃。复杂表面约束及非周期体系需后续显式支持。

### 验证

常规测试包含受控模型的工具调用、结构附件、会话隔离、确认后提交、防重复、服务重启恢复与状态查询。macOS 沙箱集成测试需在可启动系统沙箱的环境执行：

```bash
CHEM_SANDBOX_TEST=1 AIIDA_PATH=/tmp/chem-agent-tests-aiida .conda/bin/python -m pytest tests/automation/test_chat.py -q
```

登录集成测试模拟 Codex CLI 的结构化响应，实际经过 ProviderRouter、ChatGPT 响应解析、Agent 工具循环、ASE 建模与预览；默认模拟建模执行器，启用 `CHEM_SANDBOX_TEST=1` 时另测真实 macOS 沙箱。集群预览/提交在该集成测试中模拟，不消耗集群资源；已有独立 AiiDA dry-run 与真实 P100 计算验证仍保留。未登录真实 ChatGPT 账号或调用真实模型 API；模拟验收不等同于账号授权和模型效果验证。

### 会话压缩与计算记忆

聊天会在输入达到配置预算的 80% 时自动摘要已完成的旧对话。默认保留最近 4 轮；若这些历史轮次本身超出预算，可进一步减少至 0 轮，当前轮次不被摘要。过大的当前输入仍会明确报错。工具调用和返回成对保留，当前计算结果中的长日志可缩短为摘录，结构化结果和 ID 保留。

聊天下方的“会话记忆”显示估算 token 用量、摘要覆盖范围和摘要内容，并提供“立即压缩历史”。该操作使用当前登录/配置的模型，会产生模型调用；短对话没有可摘要前缀时不调用。摘要不删除原始消息或计算产物；失败会保留原记录并释放会话锁。

- `GET /sessions/{id}/context`：查看记忆和估算预算。
- `POST /sessions/{id}/context/compact`：在会话运行锁下压缩，保留原文。
- `calculation_artifacts(offset, limit)`：分页找回结构、建模、预览及提交 ID。
- `calculation_artifact(kind, artifact_id, code_offset)`：读取准确的参数、赝势溯源、提交记录或分段建模代码。记录仅在所属会话内可读，提交记录的状态是历史快照，最新状态仍查询 `calculation_job`。

Token 数是本地估算，不等于提供商计费数。ChatGPT 订阅从所选模型目录读取常规上下文窗口，保留安全余量和输出空间，按会话独立计算。套餐使用额度由服务商管理。其他接口或缺失窗口元数据时使用服务端配置。

### 一次注册，按结构准备计算

在 `/automation` 中注册赝势源（已连接服务器上的目录）和计算方案。源注册仅检查目录，不批量复制整个赝势库。源、方案保存在独立 AiiDA 数据目录的 `calculation-configuration.json`，不依赖模型摘要，也不提交到 Git。

1. 注册 PBE 赝势源：服务器、绝对目录、族名。目录布局为 `元素或变体/POTCAR`。
2. 保存计算方案：服务器、Code、队列资源、赝势源、元素映射、INCAR、固定 k 网格或倒空间间距、适用元素及原子数上限。用途与科学局限须在描述中明确。
3. Agent 调用 `calculation_setup` 发现方案，然后调用 `calculation_prepare(structure_id, protocol_id, incar?, kmesh?)`。省略方案时选择唯一默认方案；显式 k 网格优先于方案中的倒空间间距。
4. 后台按实际元素解析映射（未指定变体时用元素名），按需经 SSH 导入缺失 POTCAR 并在 AiiDA 去重。校验元素、名称、PBE 标识、族内歧义；缺失时报告缺口，不猜另一个变体。已有赝势只读本地，不重复连接服务器取文件。
5. 准备真实输入与脚本，记录方案快照、POTCAR UUID/校验和及是否本次导入。POTCAR 内容不会进入模型、聊天或公共下载接口。用户检查预览后点击确认，才能提交。

`POST /api/automation/potential-sources` 和 `/protocols` 仅供本机用户设置；服务器、软件、赝势和执行资源也可以由 Agent 按用户请求通过设置工具登记。`GET /api/automation/configuration` 提供设置界面的配置读回。已生成预览保留当时参数，修改方案后需创建新预览。

倒空间网格按 `ceil(2π|b_i| / spacing)` 计算，其中 ASE 的 `b_i` 不含 `2π`。这是初始取样方案，不证明精度已经收敛。默认映射不会自动选 `_sv` / `_pv`；磁性、带电体系、DFT+U、色散、溶剂等模型仍须按科学目标明确。PBS 的 GPU 资源语法仍需按站点适配。

### 任务状态与站内通知

启动本地后台后，每 20 秒读取已提交的会话任务，卡片与站内通知每 15 秒更新；状态来自 AiiDA，而不是模型推测。排队、运行、调度器拒绝、暂停、异常结束、未收敛和结果取回分别显示。右侧「计算」按本对话列出任务，只显示关键参数和当前状态；展开状态可看退出码、能量、收敛和子任务。展开「参数、结构与提交脚本」可逐个查看实际输入。聊天卡片不展示赝势族或赝势来源校验区；后台仍保存完整溯源。

通知和最后成功快照持久保存在 `data/aiida/task-monitor.sqlite3`。首次启动补记已有任务状态，但不为历史失败自动调用模型或提交任务。重复轮询和服务重启不会重复生成同一任务同一状态通知。同步失败会显示旧记录及错误。关闭网页不停止后台监控；关闭后台或管理机器后，通知与模型诊断停止，独立运行的 AiiDA daemon 仍按其所在机器状态执行。

Agent 可使用 `calculation_events` 查询本会话通知，再用 `calculation_job` 获取实时结果与恢复记录。页面接口为 `/api/automation/notifications`、`/api/automation/chat/{session}/events`；标记已读只影响通知，不改计算状态。环境设置 `CHEMISTRY_AUTOMATION_MONITOR_ENABLED=false` 可关闭后台监控和故障唤醒。

### 优化 → 收敛检查 → 单点 → 汇总

让聊天 Agent 选择优化方案，调用 `calculation_prepare(..., workflow="relax_static")`。预览展示两个阶段的真实 INCAR、KPOINTS、资源与脚本，并注明总计两个计算。第一版要求固定晶胞优化：`NSW>0`、`IBRION>0`、`ISIF=2`、`EDIFFG<0`。单点保持原科学参数，仅设置 `NSW=0, IBRION=-1`。

点击一次“确认整个流程并提交”后，AiiDA WorkChain 管理两个子任务并保存进度。只有优化正常退出、电子/离子收敛明确为真、能量和力有限、优化结构存在时，才把该输出结构交给单点计算。缺少证据会停下并通知，不用原始结构冒充优化结构。单点完成后再次检查电子收敛并汇总能量、最大力；这不等同于 ENCUT/k 点或模型精度已验证。已提交的独立优化任务不会被自动追加单点。

新工作流需要重新安装本地项目以注册入口，并让 AiiDA daemon 加载当前版本。常规自动化测试通过实际 AiiDA 引擎、模拟本地子进程验证顺序、结构传递及失败阻断；真实 VASP dry-run 验证两个阶段输入。未执行真实的完整两步 VASP 流程时，不应宣称端到端生产验证完成。

### 故障唤醒与有界恢复

任务出现新故障后，后台使用当前模型配置进行一次独立诊断，不需要用户发送新消息。诊断输入包括真实状态、限长日志、原参数和恢复预算；不包含 POTCAR 内容或连接凭据。模型仅返回结构化建议，不能执行 shell、提交任务或修改注册配置。

- 默认“只诊断”：可生成新的修复预览，等待用户确认；不会自动重提交。
- 提交时勾选“允许 Agent 自动修复”：每个原始任务共享 0–5 次恢复预算（默认 2 次，须显式启用）；每次墙钟上限不超过原任务的 2 倍。两步流程以整个流程的时限为基准。
- 执行器只接受原参数重试、提高 `NELM` 到不超过 240、`ALGO=Normal`，或在上述范围内延长时限。不能增加 CPU/GPU/内存、改变队列或修改结构、泛函、磁性、电荷、赝势、ENCUT、k 点和收敛阈值。
- 原任务须明确终止，或在没有调度器作业号的拒绝提交状态下暂停，才可创建替代任务。工作流仍等待子任务时不会重交整个工作流。
- 每次保留诊断、修改原因、用量、预览、原/新任务关联和预算预留。结果不明时保留同一预览并转人工核查；不会新建预览绕过防重复。可以在原任务卡片“关闭自动处理”。

当前工作流的故障恢复会从原结构重跑完整两步，不是从 WAVECAR/CHGCAR 或失败步骤续算。需要改动物理模型、超过预算、模型不可用或分析中断时，通知用户处理。日志不足时模型应停下；有界执行器限制行为范围，但不保证模型的故障判断正确。

## 体相晶格到固定层表面

`calculation_prepare(workflow="bulk_surface", surface_plan=...)` 会生成完整流程预览。
确认一次后由 AiiDA `chemistry.bulk_surface` WorkChain 推进：体相晶胞优化、收敛检查、
按实际输出晶格切表面、设置固定层、表面优化及结果检查。浏览器关闭后仍由 daemon 运行。
当前自动模板明确限定为单元素、4 原子 fcc 惯用胞的 (100) 表面；其他晶型和晶面仍可用
ASE 建模工具，但不应承诺已支持相应自动流程。

示例 `surface_plan`：`size=[2,2]`、`layers=4`、`fixed_layers=2`、
`vacuum_angstrom=20`、`kmesh=[9,9,1]`。真空指相邻周期 slab 间的总间隔，
调用 ASE 时每侧为10 Å。表面保持晶胞不变（ISIF=2），默认 EDIFFG=-0.03 eV/Å。
这些是可审阅的初始参数，不代表已经做了截断能、k 点、层数或真空收敛测试。
`bulk_job_id` 可复用本对话内已正常结束、已优化晶胞且收敛的体相，避免重复计算。
没有复用任务时，预览表面 POSCAR 仅用初始晶格展示几何；执行时会用收敛晶格重新生成。
失败会停止后续步骤并通知，表面流程目前只自动诊断，不自动重跑整个流程。

ASE 的 `FixAtoms` 不再被一律拒绝：转换为独立的 AiiDA `dynamics.positions_dof` 输入，
由 aiida-vasp 写入 POSCAR 的 Selective Dynamics。`FixScaled` 也可保留；
`FixCartesian` 只在与晶格方向约束等价时转换。不能用 Selective Dynamics 表示的键长等约束
会清楚报错。ASE 表面层标签不会再误变成 Cu1、Cu2 等 AiiDA 化学种类。

`build_structure` 默认仍写 `structure.extxyz`；含分数方向约束时用
`output_format="vasp"` 并写 `structure.vasp`。读取已有结构可用 `input.vasp`，
避免普通 ASE extxyz 对部分约束的格式限制。
`calculation_import_result(job_id=...)` 和右侧「结构」中的「用于对话」能把真实输出结构
注册回当前会话，保留来源节点和固定设置，不再只能拿初始结构继续建模。

验证包括真实 AiiDA 本地流程（模拟计算子进程）、失败阻断、复用体相不重复执行、
实际 aiida-vasp POSCAR 输出的固定标记，以及原 #60 的真实输入 dry-run。
这些验证没有新提交远端 VASP 任务，实际表面优化仍需确认卡片后运行。

完成任务可直接查看或下载优化结构，无须先导入。下载接口只读当前会话任务的最终 EXTXYZ；重复导入同一输出复用已有会话结构。聊天附件、建模产物、预览 POSCAR 和优化结果共用右侧「结构」查看器。

### 结果回到聊天与 CO 吸附方案

任务监控记录新的完成或失败事件后，后台按原会话的模型设置分析。失败优先复用故障诊断；聊天仅保存助手的结果及建议，不保存内部唤醒词。正在进行的用户回合结束后再写入；事件键防止刷新、重启造成重复消息。模型不可用时保留事实通知。不会补发历史已处理事件。

对 fcc(100) 的 CO 吸附，使用 `calculation_prepare(workflow="bulk_surface",surface_plan=...)`，在方案中设置 `co_sites=["top","bridge","hollow"]`。依次优化体相和洁净表面，从同一个收敛 slab 生成 C 朝下的初始构型；气相 CO（独立立方盒、Γ 点）与各位点作为独立 AiiDA 子任务一批提交。点击步骤可查看或提出修改；修订通过 `revises_preview_id` 引用旧版，两版不会同时可确认。吸附能单位 eV/CO，采用同一能量字段计算 E(slab+CO) − E(slab) − E(CO)。位点名称指初始位置，优化后的占位需查看各分支的最终结构。

并行阶段有分支失败时保留通过检查的分支及洁净参照，只对未通过的分支生成修复方案。一次恢复批次消耗原流程的一次预算。体相或洁净表面阶段失败、证据不足或更改超出授权时，留给用户决定。工作流入口变更后需重新安装本地 editable 项目并更新 AiiDA 工作进程。

### 通用可执行方案（2026-11-02）

新研究流程不必扩写固定 WorkChain。Agent 使用 `calculation_plan_save` 保存同一份 DAG，
右侧展示和后台执行均读取该记录；`calculation_plan_get` 可读回版本、节点、代码、参数和实际输出。
草稿修订需传 `plan_id` 与 `expected_revision`，保存旧版本和变更节点；确认绑定当前版本，
已确认定义不可静默修改。单任务和历史固定 WorkChain 保持兼容。

节点支持三类操作：

- `model`：执行 Agent 写的 ASE Python，从输入别名 `.vasp` 读取结构，输出 `structure.vasp`。
- `vasp`：从注册 protocol 生成已冻结的 INCAR、k 点规则、资源、Code/服务器配置。
  显式设置电子、离子收敛门槛；就绪时才用实际结构导入赝势、生成输入和提交。
- `analysis`：执行 Python，读取 `inputs.json` 中各别名的 `result`、`files`，写 `result.json`。
  `passed=false` 作为检查失败，阻止依赖它的后续节点；其它独立分支仍推进。

输入引用已有会话 `structure_id` 或 `{node: "上游ID", output: "structure"/"result"}`。
输入引用自动补成依赖边，并在保存时验证缺失引用、环和输出类型。后续切面、吸附建模仅在上游成功后
读取实际输出，未完成时 UI 明确显示等待，不伪造未来 POSCAR。普通草稿可显式设置 `max_parallel`，不设置时无应用在途数上限；授权自动模式总是就绪即提交，实际并发由调度器分配。

通用编排运行在常驻 Python 后台，AiiDA 持久管理每个远端作业。关闭网页不影响推进；
后台服务停机时 AiiDA 作业继续，图的后续步骤在服务恢复后继续。当前使用单个 Web worker，
不要把同一个数据目录交给多个编排进程。每次提交前保存 preview_id，恢复时复用 AiiDA 的幂等提交；
结果不明的保留原 ID 并报告错误，不新建重复作业。暂停只停止启动后续节点，不取消已提交作业。
要让 Agent 自行设计并启动计算，聊天消息首行单独写 `/auto low`（也可指定其他已注册服务器支持的队列），下一行写研究目标、参数与资源。授权只适用于这一条消息和它创建的图，其他对话和普通草稿仍需确认；同一请求不会因重复保存而启动两张图。大型流程可以使用 `calculation_plan_program` 编写 Python 循环生成 PlanDraft，保存后展示和执行相同的节点及依赖。

大型图可以在生成程序里用 runpy.run_path("plan_writer.py")["PlanWriter"] 定义模型、计算和分析节点。建模/分析直接传入带内部 import 的普通 Python 函数，帮助器保存函数源码，避免手工嵌套多行字符串。生成器失败返回 program_id；calculation_plan_source 可分段读源码，下一次 program 调用通过 previous_id 将原程序暂存为 previous.py，允许局部修复。

自动图的失败节点会唤醒当前会话模型，提供失败代码、错误和作业编号，可调用计算文件工具查证，再用 `calculation_plan_repair` 修改建模/分析代码或 VASP 设置。修复沿用输入、依赖、资源与队列，保留成功节点和原尝试；最多两次/节点。未确认退出或提交归属不明时不另起作业。隐藏唤醒提示，只保存分析回复和可审计的工具记录。

文件访问不依赖扩展名白名单：`calculation_files` 支持会话所属任务的 retrieved/remote 目录列举、
文本分段读取；`calculation_analyze` 支持把指定相对路径或 `files=["*"]` 的全部文件复制到分析工作区，
用 ASE/numpy/自写解析器处理二进制、大文件或新格式。图的 analysis 节点使用 `result_files` 和
`file_source` 获取同样的文件。文件流式复制，不通过模型上下文中转，没有固定 32 MiB 输入上限。
远端运行中数据可能仍在写入，代码需自行检查完整性。文件访问限定任务目录；禁止跨目录符号链接，
并排除 archive。原始任务文件不会被分析代码改写；POTCAR 内容不作为工具返回文本自动发送给模型。

分析代码继续使用离线隔离 Python（可读当前 Python 环境和本步骤工作目录），默认 CPU 300 秒、
墙钟 600 秒、每个生成文件 64 MiB；建模沿用 20 秒 CPU/30 秒墙钟。输入文件不受输出文件大小限额影响。
这不是任意 SSH shell，远端提交由 AiiDA 负责。文件读取范围和代码执行资源边界不等于限定科学分析类型。

验证覆盖对话创建/修订、旧版本确认拒绝、实际输出传递、并行上限、暂停恢复、服务重启、提交中断幂等、
失败分支阻断、离线分析、任意二进制文件复制以及右侧结构查看。测试中的调度器作业为模拟，不表示新的
VASP 科学任务已通过实算验收。

## 通用 Slurm 节点与文件依赖

通用任务使用 `kind: "job"`，直接通过已登记 SSH 连接提交到 Slurm，**不调用 AiiDA、
不要求 AiiDA profile 或 daemon**。原有 `vasp` 专用节点保持原执行方式。
应用仍需启用本地自动化；数据目录暂沿用 `AIIDA_PATH/chat`，这是存储位置，不代表通用任务依赖 AiiDA。
服务器需要 Bash、Python 3、`sbatch`、`squeue`、`sacct`、`scancel`；SSH 主机信任与认证沿用设置。

Agent 通过 `calculation_job_hosts` 获取 SSH 别名，再用现有 `calculation_plan_save/program`
设计流程。以下只是接口示例，平台没有内置 LAMMPS/GROMACS 计算方案：

```json
{
  "id": "minimize",
  "title": "结构优化",
  "kind": "job",
  "job": {
    "host": "cluster-b",
    "workdir": "~/.chem-agent/jobs",
    "script": "source /path/to/environment.sh\nsrun lmp -in input.lmp",
    "resources": {"queue": "low", "nodes": 1, "ranks_per_node": 1,
                  "wallclock_seconds": 3600, "memory_mb": 2048},
    "files": {"input.lmp": "实际输入由 Agent 编写"},
    "retrieve": ["log.lammps", "final.data", "restart.*"],
    "required_outputs": ["final.data"]
  },
  "files": [{"node": "build", "path": "initial.data", "target": "initial.data"}]
}
```

- `job.script` 是计算节点执行的 Bash 内容，可加载软件环境、调用 MPI、运行 Python 或其他程序。
  用 `srun/mpirun` 启动计算，不在其中再次 `sbatch` 一个脱离监控的子任务。
- `scheduler_commands` 可填写额外的 `#SBATCH` 指令，例如 QOS、constraint、CPU 线程、作业数组。
  平台管理的作业名、输出位置、队列与基础资源字段优先，以便回执与监控保持一致。
- `job.files` 保存文本输入。二进制模型权重、拓扑、轨迹和重启文件使用节点的 `files` 引用。
  每项指定 `node`、`job_id`、`file_id` 之一，指定源 `path` 与目的 `target`。
  `file_id` 必须是当前对话附件；`job_id` 必须是当前对话的作业。
- `files.node` 自动形成真实 DAG 依赖；建模、分析和通用任务均可消费这些文件。
  专用 VASP 节点继续使用原结构/参数接口；需要自定义 VASP 文件脚本也可使用 `job`。
- 本地代码通过 `file_inputs.json` 查询文件映射，直接打开 `target`。
  通用任务的结构输入以 `alias.extxyz` 保存，路径写入 `inputs.json`。
  文件不经大模型传输；同一 SSH 主机的通用计算节点间直接在远端复制文件，避免大轨迹/权重绕行本机。
  本地分析优先复用已取回文件，未取回文件可从远端读取；跨服务器传递经本地流式暂存。
- `retrieve` 支持文件路径与 glob；`required_outputs` 是必须存在的确切文件名。
  结束后保存每个尝试的独立产物、大小、SHA256、调度器作业号和退出信息。
  `calculation_job_files` 可列文件、分段读日志或查询状态。

### 可靠性与 Agent 后续处理

提交前持久保存唯一任务编号，远端在 `sbatch` 前写 reservation；响应丢失时先核对回执，
再按唯一作业名查 Slurm，结果仍不明确时保持 uncertain，不盲目重投。
`squeue` 消失后继续核对 `sacct` 和脚本退出码；没有记账结果不视为成功。
数组任务汇总各元素的终态与独立退出标记。正常退出和要求文件存在只表示执行完成，科学判断交给分析。

配额拒绝进入等待；只有相同 SSH 主机、账号和分区的**已接单任务完成事件**才能释放一个等待项。
释放凭据持久保存且只能消费一次，读取状态不会重投。统计通用任务管理器记录的完成事件，
包括通过下述接管工具登记的外部作业；未登记的外部任务不参与。

完成/失败事件持久保存，自动启动原会话中的实际 Agent 工具循环；内部提示词不作为用户消息展示。
用户正在对话时保留事件等待。Agent 可以读文件、分析、修复失败节点，也可以在 `/auto QUEUE`
授权的流程中通过 `calculation_plan_extend` 追加新节点；已有节点及输出保留。方案的 `repair_limit` 指定每个节点允许自动修复的次数（默认 2，0 禁止自动重试）。
普通确认方案按已确认步骤执行，
超出原方案的计算需要新授权。稳定节点 ID 防止重复追加。

通用任务的续跑通过下述持久等待与事件收件箱恢复；未知工具结果不自动重放。
AiiDA 的既有失败诊断仍使用原有 rediagnose 路径。后台必须常驻；主机睡眠期间集群作业继续运行，
查询、取回和模型回合在后台恢复后继续。

本实现有本地模拟 Slurm 与真实 Agent 工具循环回归测试。早期 cluster-b 检查曾成功找到命令，随后 SSH 连接被拒绝；
后续本地真实 Slurm/ORCA 验收见下节，尚不代表远端 cluster-b 或特定 LAMMPS/GROMACS/势函数版本已验证。

调度接口依据：[Slurm sbatch](https://slurm.schedmd.com/sbatch.html)、[Slurm sacct](https://slurm.schedmd.com/sacct.html)。


## 持久等待与会话续跑

Agent 用 `calculation_wait_register` 注册一次性条件，`calculation_wait_control` 查询、取消或
在配置修复后重试模型阶段。规则与事件保存在应用的 SQLite 数据库中，不依赖聊天页面或内存定时器。
条件可嵌套 `all` / `any`；叶子是节点 `state` 或输出 `file`，不限定计算软件或科学步骤。
`state` 观察方案节点状态（例如 completed/failed/blocked），不是 Slurm 分区中的全部作业。
文件条件观察已发布产物；通用 Slurm 节点还可只读检查运行中作业目录，默认每 30 秒一次，
`poll_seconds` 可调。文件出现不等于写入完成，Agent 可以等待自己脚本最后写出的完成标记。
连接失败视为未知；不会当作文件存在，也不会因此调用模型。

例如，Agent 可以自行调用：

```json
{
  "plan_id": "实际方案 UUID",
  "key": "compare-candidates-v1",
  "condition": {
    "op": "all",
    "conditions": [
      {"op": "state", "node": "candidate_a", "states": ["completed", "failed", "blocked"]},
      {"op": "state", "node": "candidate_b", "states": ["completed", "failed", "blocked"]}
    ]
  },
  "instruction": "核对实际输出，比较候选结构；根据结果决定是否补算和如何继续。",
  "replace_default": true
}
```

`key` 在会话与方案内幂等：重试同一注册返回原记录；换条件需要新 key。`replace_default=true`
替代所覆盖节点的逐项完成通知，适合并行组汇总；false 则额外保留逐项通知。
一次性等待匹配后不重新求值，结果和事件同一事务提交；取消或过期不会重新启用其覆盖节点的默认通知。
需要再次观察修复后的尝试时注册新 key。匹配前可设 `expires_at`（Unix 时间）；已匹配的事件不因时间流逝丢失。

没有自定义等待时，通用任务仍默认结束即唤醒；同一轮检测到的完成事件合并处理。
重复观察使用同一事件 ID，不制造新回合。自定义条件目前限定当前会话中一个方案的节点；
不支持任意跨会话或外部未登记作业。建模/分析/VASP 节点也可以被显式等待条件引用。
现有 VASP 解读/诊断机制尚未迁移进此收件箱，不因此声称所有旧后台路径都具备相同恢复保证。

### 恢复和一致性

- 注册、匹配、接单和处理分别有持久状态。会话繁忙时保持 pending；不会丢弃事件或抢占用户回合。
- 事件接单与会话 run 的创建在同一 SQLite 事务中；回复保存与事件 completed 在同一事务中。
  即使在回复提交后、方案 JSON 更新前退出，也不会再次执行这条事件。
- 工具批次开始前记下调用意图；全部工具结果确认后保存消息、结果、用量与模型检查点。
  服务重启或模型限流后，从已确认的模型边界继续，不重新执行已完成工具。
- 429、可识别的订阅用量限制、5xx 和暂时连接失败使用持久化退避，遵守 Retry-After。
  到期仅尝试恢复，不声称额度一定恢复；后续仍失败就继续退避。认证/配置等非暂时错误进入 blocked，
  可通过 `calculation_wait_control(action="retry", event_id=...)` 在修复后恢复。
- 在工具执行期间中断，未观察到完整结果时进入 uncertain；保存 tool_intent 供 Agent 查询和对账，
  不自动重放任意 shell/写入操作，也不把不明状态当成功。当前这类未知副作用仍需核对后另起处理，
  不能用 retry 强行重放；通用 Slurm 的实际提交层另外通过唯一任务编号、远端回执和调度器对账防止重投。
- 投递前检查方案停止/暂停状态与任务尝试身份，旧尝试事件不会错误作用于替换任务。
  用户停止取消等待与未处理事件；服务正常退出仅保存恢复点，两者不同。
- 内部消息作为带 event_id/type 的后台上下文进入原 Agent 工具循环，不存成可见用户消息。
  只保存 Agent 的回复和工具记录；等待条件不会授予额外计算授权。

回归覆盖事务回滚、进程重启、繁忙会话、分组通知、旧事件失效、等待过期、运行中文件观察、
429 退避，以及真实应用 Agent 注册等待和在追加步骤后恢复。模型响应和 Slurm 在测试中模拟，
不会消费实际模型额度或向 cluster-b 提交作业。

## 接管终端提交的已有 Slurm 作业

Agent 可以自行编写输入和提交脚本、通过 `remote_shell` 执行 `sbatch --parsable`，
取得真实作业号后调用 `calculation_job_adopt`。登记不执行 sbatch、不改写作业目录，
也不要求安装 AiiDA、使用特定计算软件或插入本系统的退出标记。

```json
{
  "host": "已登记的SSH别名",
  "scheduler_job_id": "实际Slurm作业号",
  "title": "ORCA 几何优化",
  "retrieve": ["orca.out", "input.xyz"],
  "required_outputs": ["orca.out"],
  "instruction": "读取实际输出核对几何收敛；使用最终结构继续已授权的频率计算。"
}
```

返回 `job_id`、`plan_id`、`node_id`。后续已有作业可用 `plan_id` 登记到同一方案，
或由 Agent 注册 all/any 分组等待。原始输出由 `calculation_job_files` 读取，
文件依赖可引用这些 job/node ID。监控元数据不伪造原始脚本、资源或未知软件参数。

- 接管时查询 `scontrol show job`，确认集群、提交时间、账号、作业号和绝对 WorkDir。
  可填写 `remote_directory` 与 `submitted_at` 强制核对；提交时间应取调度器的 SubmitTime，
  不应假定提交前执行的 `date` 与 SubmitTime 完全相同。
- 控制器记录过期时查询 `sacct -D`；有多个作业号实例时需指定提交时间。
  后续轮询固定原始身份，Slurm 作业号被复用、连接失败、记账缺失均不推断完成。
- 已完成但尚未登记的作业也可接管，登记后的下一轮观察会产生完成事件。
  重复/并发登记返回同一绑定；先持久化身份、再写方案，重启可补全中断的登记，绝不重投。
- 支持普通作业和具体数组元素（例如 `123_4`）；数组父编号、范围、job step 和异构组件
  不能当作一个普通作业接管。需要数组聚合时逐项登记，再注册分组等待。
- 工作目录可列出/读取任意格式产物；文件依赖和取回沿用现有目录边界。
  调度器 COMPLETED 且 ExitCode=0:0 只说明作业成功退出，软件收敛由 Agent 读取输出判断。
- 结束事件进入同一个 SQLite 收件箱，自动唤醒原对话，隐藏后台提示词。
  用户停止/暂停方案仍优先；接管本身不会授予额外计算权限。
- 修复接管节点时必须提供新尝试的脚本和资源；监控占位数据不能作为提交输入重放。
  Agent 也可通过终端在新目录提交修复作业并接管，保留原失败作业证据。
  任意终端命令本身没有自动幂等保证；sbatch 回执不明时先查询调度器，不盲目重试。

本地真实联调脚本：`scripts/verify_adopted_orca.py`。配置区指定现有 Docker Slurm、
共享目录、本机 ORCA 和模型配置路径。它使用真实应用 Agent、真实队列及真实 ORCA 输出，
通过共享文件桥接 macOS ORCA 与 Linux 队列，Docker exec 替代 SSH 传输；
该联调不等于 Linux ORCA 安装或远端 SSH 链路验收。`resume_test` 可续接已保存的测试会话，
不重建已完成作业。数据保存在被 Git 忽略的 `data/adopt-orca-validation/`。
