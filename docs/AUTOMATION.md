# AiiDA 自动化任务与聊天工作流

目标：在网页选择服务器、提供一个结构，预览 AiiDA 生成的输入与调度脚本，明确提交后查看真实任务状态。

当前面向本地单用户。状态监控与工作流推进不调用模型；新发生的任务故障可唤起已配置的模型诊断，产生相应模型用量。实际重提交由明确授权的恢复策略约束。

## 当前能力与边界

| 功能 | 实现及验证范围 |
| --- | --- |
| 服务器 | 使用后台机器的 SSH 别名、密钥及已信任主机配置；网页不接收私钥或密码 |
| 连接检查 | AiiDA SSH、调度器查询、工作目录现有上级目录的写权限；不提交或创建远端文件 |
| 调度器 | Slurm、PBSPro/OpenPBS、Torque 的 AiiDA 插件；三种脚本均有真实 dry-run 测试 |
| 结构 | POSCAR、CIF、EXTXYZ；单帧、完整周期晶胞、最多 10,000 原子 |
| 输入保护 | 拒绝非有限坐标、原子约束、初始磁矩/电荷；暂不做可能丢失信息的自动转换 |
| 联通测试 | 自带 CalcJob：上传 POSCAR，在计算节点记录 hostname 和 SHA256，取回并校验；不做科学计算 |
| VASP | aiida-vasp CalcJob；显式 INCAR、k 点、赝势族及映射；远端 Code 和环境脚本注册 |
| GPU | Slurm 的 GPU 数量及可选类型；PBS GPU 语法需按具体站点适配 |
| 预览 | AiiDA 原生 dry-run；显示 POSCAR、INCAR、KPOINTS、提交脚本；不向网页返回 POTCAR |
| 提交 | 固定预览 ID；持久化预留记录；重复请求返回同一 AiiDA 节点 |
| 状态 | AiiDA process 状态、scheduler 状态、作业号、退出码、更新时间及输出；最近 100 个任务 |

已接入聊天工具、任务通知、两步 WorkChain 与受限自动恢复，使用方式见下文。尚未实现批次节流、优先级策略、取消任务、多用户计算权限或科学精度验收。不要把流程联通测试等同于批量生产能力。

## 安装和启动

创建独立环境，然后安装本地包及自动化依赖：

```bash
conda create -n chem-agent python=3.12 pip
conda activate chem-agent
python -m pip install -e '.[automation,dev]'
python scripts/setup_automation.py
python scripts/start_automation.py
```

这里的 pip 命令用于安装本地项目的可选依赖组；Python 环境由 Conda 管理。不要直接套用旧的 Windows `constraints.txt` 锁定自动化环境。

打开 <http://127.0.0.1:8766/automation>。`setup_automation.py` 创建独立 AiiDA profile，并启动一个 daemon worker；重复运行会保留已有数据库。`start_automation.py` 显式启用本地自动化并绑定 loopback。可编辑脚本顶部配置，不需要命令行参数。

默认的 `AIIDA_PATH` 位于项目 `data/aiida`，profile 名为 `chem-agent`。可用 `CHEMISTRY_AIIDA_PATH` 和 `CHEMISTRY_AIIDA_PROFILE` 指定；若改 profile 名，需同步修改初始化脚本顶部。该路径不使用或覆盖用户的默认 AiiDA 配置。

当前采用 AiiDA 2.9 的 SQLite + ZeroMQ 来验证流程。大量并发计算应迁移到常驻管理机器，使用 PostgreSQL 及合适的 broker，并实现批次限流。参见 [AiiDA 安装能力说明](https://aiida.readthedocs.io/projects/aiida-core/en/stable/installation/guide_quick.html)。

## 第一次提交

1. 在后台所在机器上先确认已有 SSH 别名能正常免交互登录，并人工核对主机指纹。
2. 在网页保存服务器：名称、SSH 别名、调度器、独立远端工作目录。
3. 点击“测试连接”。工作目录未创建时只检查最近的现有上级目录；实际提交时 AiiDA 会创建作业目录。
4. 选择“结构联通测试”，导入结构或填入页面明确标注的 Cu 测试结构。
5. 填写站点允许的队列。联通测试限制为单节点、单进程、最多 120 秒、不申请 GPU。
6. 生成预览并检查资源与脚本，再点击“提交到 AiiDA”。先出现 AiiDA ID，调度器实际接受后才出现作业号。
7. 在任务详情确认结束状态及 `report`。成功探针必须同时验证远端标记和 POSCAR 的 SHA256。

预览里的 `aiida-None` 是未存储 dry-run 节点的占位作业名称，真实提交后由 AiiDA 节点编号替换。预览不等于提交。

## VASP 配置

注册 Code 时提供真正的 `vasp_std` 绝对路径及可选环境脚本；注册只检查文件存在，不执行 VASP。环境脚本会通过 `source` 写入计算节点的提交脚本。MPI 默认使用 `mpirun -np {tot_num_mpiprocs}`，需要与站点环境匹配。

赝势须由有使用权限的用户导入该 AiiDA profile，页面填写对应赝势族名称及元素映射。导入可使用 aiida-vasp 的 `PotcarData.upload_potcar_family`，参见 [官方赝势配置文档](https://aiida-vasp-plugin.readthedocs.io/en/v5.x/howto/setup_potentials.html)。不要把 POTCAR、VASP 可执行文件或许可证上传到代码仓库。

INCAR 必须显式填写，程序不会替你选择泛函、色散、ENCUT、ISPIN、MAGMOM 或收敛阈值。单点应明确设置 NSW=0 等参数；结构优化需要匹配的 IBRION、NSW、ISIF、EDIFFG。GPU 数量、GPU 类型、MPI 进程数也由用户按程序构建和站点要求指定。

单任务提交 CalcJob；优化后单点使用本项目的 `chemistry.relax_static` WorkChain。恢复执行器创建独立的新任务，保留失败记录；不包含 aiida-vasp VaspWorkChain 的检查点重启策略。VASP 返回正常仍需独立核对电子/离子收敛和研究所需的科学标准。

## 实现结构

- `automation/models.py`：输入和资源的严格校验。
- `automation/routes.py`：本地 API；拒绝跨源、非 loopback 和共享账号模式。
- `automation/service.py`：串行启动隔离 Python 工作进程，避免 AiiDA 的全局 profile/阻塞 I/O 影响聊天事件循环。
- `automation/worker.py`：受限 JSON 操作分派与日志隔离。
- `automation/backend.py`：Computer/Code、dry-run、提交、查询和持久化防重复记录。
- `automation/probe.py`：真正的 AiiDA CalcJob 与结果 Parser。
- `web/automation/`：独立网页，复用 FastAPI 服务；通过首页导航进入。

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

聊天会在输入达到配置预算的 80% 时自动摘要已完成的旧对话。默认保留最近 4 轮；若这些历史轮次本身超出预算，会进一步减少到至少 1 轮，当前轮次不被摘要。过大的当前输入仍会明确报错。工具调用和返回成对保留，当前计算结果中的长日志可缩短为摘录，结构化结果和 ID 保留。

聊天下方的“会话记忆”显示估算 token 用量、摘要覆盖范围和摘要内容，并提供“立即压缩历史”。该操作使用当前登录/配置的模型，会产生模型调用；短对话没有可摘要前缀时不调用。摘要不删除原始消息或计算产物；失败会保留原记录并释放会话锁。

- `GET /sessions/{id}/context`：查看记忆和估算预算。
- `POST /sessions/{id}/context/compact`：在会话运行锁下压缩，保留原文。
- `calculation_artifacts(offset, limit)`：分页找回结构、建模、预览及提交 ID。
- `calculation_artifact(kind, artifact_id, code_offset)`：读取准确的参数、赝势溯源、提交记录或分段建模代码。记录仅在所属会话内可读，提交记录的状态是历史快照，最新状态仍查询 `calculation_job`。

Token 数是本地估算，不等于提供商计费数。上下文预算目前来自服务端 `CHEMISTRY_CONTEXT_*` 设置，不自动猜测各供应商的模型窗口。

### 一次注册，按结构准备计算

在 `/automation` 中注册赝势源（已连接服务器上的目录）和计算方案。源注册仅检查目录，不批量复制整个赝势库。源、方案保存在独立 AiiDA 数据目录的 `calculation-configuration.json`，不依赖模型摘要，也不提交到 Git。

1. 注册 PBE 赝势源：服务器、绝对目录、族名。目录布局为 `元素或变体/POTCAR`。
2. 保存计算方案：服务器、Code、队列资源、赝势源、元素映射、INCAR、固定 k 网格或倒空间间距、适用元素及原子数上限。用途与科学局限须在描述中明确。
3. Agent 调用 `calculation_setup` 发现方案，然后调用 `calculation_prepare(structure_id, protocol_id, incar?, kmesh?)`。省略方案时选择唯一默认方案；显式 k 网格优先于方案中的倒空间间距。
4. 后台按实际元素解析映射（未指定变体时用元素名），按需经 SSH 导入缺失 POTCAR 并在 AiiDA 去重。校验元素、名称、PBE 标识、族内歧义；缺失时报告缺口，不猜另一个变体。已有赝势只读本地，不重复连接服务器取文件。
5. 准备真实输入与脚本，记录方案快照、POTCAR UUID/校验和及是否本次导入。POTCAR 内容不会进入模型、聊天或公共下载接口。用户检查预览后点击确认，才能提交。

`POST /api/automation/potential-sources` 和 `/protocols` 仅供本机用户设置；模型不能注册任意文件源或静默改变默认资源。`GET /api/automation/configuration` 提供设置界面的配置读回。已生成预览保留当时参数，修改方案后需创建新预览。

倒空间网格按 `ceil(2π|b_i| / spacing)` 计算，其中 ASE 的 `b_i` 不含 `2π`。这是初始取样方案，不证明精度已经收敛。默认映射不会自动选 `_sv` / `_pv`；磁性、带电体系、DFT+U、色散、溶剂等模型仍须按科学目标明确。首版结构约束、任意复杂工作链和 PBS GPU 配置的原有限制仍适用。

### 任务状态与站内通知

启动本地后台后，每 20 秒读取已提交的会话任务，卡片与站内通知每 15 秒更新；状态来自 AiiDA，而不是模型推测。排队、运行、调度器拒绝、暂停、异常结束、未收敛和结果取回分别显示。失败日志、能量与电子/结构收敛结果出现在任务卡片。聊天卡片不展示赝势族或赝势来源校验区；后台仍保存完整溯源。

通知和最后成功快照持久保存在 `data/aiida/task-monitor.sqlite3`。首次启动补记已有任务状态，但不为历史失败自动调用模型或提交任务。重复轮询和服务重启不会重复生成同一任务同一状态通知。同步失败会显示旧记录及错误。关闭网页不停止后台监控；关闭后台或管理机器后，通知与模型诊断停止，独立运行的 AiiDA daemon 仍按其所在机器状态执行。

Agent 可使用 `calculation_events` 查询本会话通知，再用 `calculation_job` 获取实时结果与恢复记录。页面接口为 `/api/automation/notifications`、`/api/automation/chat/{session}/events`；标记已读只影响通知，不改计算状态。环境设置 `CHEMISTRY_AUTOMATION_MONITOR_ENABLED=false` 可关闭后台监控和故障唤醒。

### 优化 → 收敛检查 → 单点 → 汇总

让聊天 Agent 选择优化方案，调用 `calculation_prepare(..., workflow="relax_static")`。预览展示两个阶段的真实 INCAR、KPOINTS、资源与脚本，并注明总计两个计算。第一版要求固定晶胞优化：`NSW>0`、`IBRION>0`、`ISIF=2`、`EDIFFG<0`。单点保持原科学参数，仅设置 `NSW=0, IBRION=-1`。

点击一次“确认整个流程并提交”后，AiiDA WorkChain 管理两个子任务并保存进度。只有优化正常退出、电子/离子收敛明确为真、能量和力有限、优化结构存在时，才把该输出结构交给单点计算。缺少证据会停下并通知，不用原始结构冒充优化结构。单点完成后再次检查电子收敛并汇总能量、最大力；这不等同于 ENCUT/k 点或模型精度已验证。已提交的独立优化任务不会被自动追加单点。

新工作流需要重新安装本地项目以注册入口，并让 AiiDA daemon 加载当前版本。常规自动化测试通过实际 AiiDA 引擎、模拟本地子进程验证顺序、结构传递及失败阻断；真实 VASP dry-run 验证两个阶段输入。未执行真实的完整两步 VASP 流程时，不应宣称端到端生产验证完成。

### 故障唤醒与有界恢复

任务出现新故障后，后台使用当前模型配置进行一次独立诊断，不需要用户发送新消息。诊断输入包括真实状态、限长日志、原参数和恢复预算；不包含 POTCAR 内容或连接凭据。模型仅返回结构化建议，不能执行 shell、提交任务或修改注册配置。

- 默认“只诊断”：可生成新的修复预览，等待用户确认；不会自动重提交。
- 提交时勾选“允许 Agent 自动修复”：每个原始任务合计最多自动重试 2 次；每次墙钟上限不超过原任务的 2 倍，因此含原任务的授权计算时限合计最多 5 倍。两步流程以整个流程的时限为基准。
- 执行器只接受原参数重试、提高 `NELM` 到不超过 240、`ALGO=Normal`，或在上述范围内延长时限。不能增加 CPU/GPU/内存、改变队列或修改结构、泛函、磁性、电荷、赝势、ENCUT、k 点和收敛阈值。
- 原任务须明确终止，或在没有调度器作业号的拒绝提交状态下暂停，才可创建替代任务。工作流仍等待子任务时不会重交整个工作流。
- 每次保留诊断、修改原因、用量、预览、原/新任务关联和预算预留。结果不明时保留同一预览并转人工核查；不会新建预览绕过防重复。可以在原任务卡片“关闭自动处理”。

当前工作流的故障恢复会从原结构重跑完整两步，不是从 WAVECAR/CHGCAR 或失败步骤续算。需要改动物理模型、超过预算、模型不可用或分析中断时，通知用户处理。日志不足时模型应停下；有界执行器限制行为范围，但不保证模型的故障判断正确。
