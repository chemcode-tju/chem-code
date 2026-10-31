# AiiDA 自动化任务：第一阶段

目标：在网页选择服务器、提供一个结构，预览 AiiDA 生成的输入与调度脚本，明确提交后查看真实任务状态。

此阶段面向本地单用户。它不依赖模型 API，也不会自动调用收费模型。已有聊天功能保持独立。

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

尚未实现批次节流、优先级策略、自动修复、WorkChain 多步流程、取消任务、聊天工具调用、多用户权限或科学结果验收。不要把单任务联通测试等同于批量生产能力。

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

当前直接提交 CalcJob，不包含 VaspWorkChain 的自动重启处理。VASP 返回正常仍需独立核对电子/离子收敛和研究所需的科学标准。

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
