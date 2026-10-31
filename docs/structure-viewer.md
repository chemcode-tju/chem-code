# 结构与轨迹查看器

在 Chem-Agent 首页点击“结构与轨迹”，或访问同一服务的 `/structures`。选择“本地文件”或直接拖入文件；可与化学助手分别打开在两个浏览器标签页。无需模型 Key。启用账号模式时，页面及转换接口均需要登录。

## 支持格式

| 文件 | 读取方式 | 说明 |
| --- | --- | --- |
| `.xyz`、`.extxyz` | 浏览器本地按需读取 | 多帧、变化原子数、extxyz 晶胞/PBC/原子 ID/质量 |
| POSCAR、CONTCAR、`.vasp`、`.poscar`、`.contcar` | 浏览器本地按需读取 | VASP 5/6，要求元素名称行，支持 Direct/Cartesian、Selective dynamics |
| XDATCAR、XDATCAR.1、`.xdatcar` | 浏览器本地按需读取 | 固定头部的多帧轨迹；重复头部/变胞拼接文件应先转换为 extxyz |
| ASE `.traj` | 浏览器本地按需读取 | ASE ULM 轨迹；不是任意后缀为 traj 的文本 |
| LAMMPS `.dump`、`.lammpstrj` | 当前服务转换为 extxyz | 文本 dump，正交/倾斜晶胞，Cartesian/scaled 坐标，多帧，按 atom ID 排序 |
| LAMMPS `.data`、`.lmp` | 当前服务转换为 extxyz | 可选择 atomic/full/charge/molecular，默认读取 Atoms 标题 |
| GROMACS `.gro` | 当前服务转换为 extxyz | 单帧或串接多帧；标准固定列坐标，nm 自动转换为 Å |
| `.pdb` | 当前服务转换为 extxyz | 结构与多 MODEL 坐标 |

其他文件名可在“文件格式与导入选项”中明确指定格式。暂不读取 GROMACS XTC/TRR/TPR、LAMMPS 二进制 dump、VASP 4 缺失元素名的结构。

### LAMMPS 的元素、单位和原点

数字 `type` 是力场类型，不是原子序数。dump 缺少 `element` 列时，必须填写例如 `Cu O H`，依次对应 type 1、2、3；data 始终要求映射。不会把 type 1 默认为 H，也不会用质量猜测元素。提供 `element` 列的 dump 可以直接读取。

导入选项支持 `units metal` 和 `units real`，两者的长度单位均为 Å；其他单位制请先转换。原子 ID 保留，乱序 dump 会按 ID 排序。晶胞原点整体平移到零，保持原子之间的相对位置与晶胞关系。data 不包含边界条件，按三维周期显示；非周期体系请勿启用 Wrap。

GRO 不提供独立元素字段，ASE 根据原子名推断元素，请核对显示的元素。无法识别的虚拟位点/粗粒化原子会报错，而不是冒充真实元素。GRO 序号可能回绕，因此跨帧对应依据元素和顺序；原子排序变化的 GRO 不适合做质心漂移校正。GRO 盒尺寸也会从 nm 转为 Å。

## 使用

- 左键旋转、右键平移、滚轮缩放；Shift + 点击/拖动切换原子选择。
- 选择 2/3/4 个原子显示距离/角度/二面角，更多原子显示组成。切换选择不会重置镜头。
- 时间轴支持逐帧、跳帧和播放；在索引完成前帧数显示 `+`。
- Wrap 和质心漂移校正只改变显示，原文件不被修改。漂移校正要求连续、未折回坐标以及可靠的原子对应关系。
- 快照经浏览器下载，可选 PNG/JPEG/PDF、DPI、透明背景和裁剪。浏览器开始下载不等于已确认磁盘保存成功。

## 数据与资源边界

本地读取格式不会上传坐标。转换格式仅上传到当前 Chem-Agent 服务，在请求内转换，不保存原始文件或转换结果，不发送给 DeepSeek。远端部署时，这意味着转换格式会离开浏览器并传输到你的服务器。

本地读取保留原扩展的字节索引与 LRU 帧缓存（最多 16 帧、估算 64 MiB；单个大帧可能超过预算）。转换格式限上传 32 MiB、每帧 100,000 原子、10,000 帧、累计 1,000,000 原子、输出 64 MiB；转换完成后才显示第一帧。大型 LAMMPS/GRO 轨迹建议先转为 extxyz，再本地读取。

转换输出只保留查看所需的元素、位置、晶胞、PBC、质量和可用的 ID，不是完整的计算结果格式转换工具。力、能量、速度、拓扑等字段不作为分析数据输出。当前未接入势能计算器、几何优化、原生 macOS 保存助手或 FFmpeg 动画导出。

## 来源与验证

查看器复用 `liyang-tju/extxyz-extension` 的 Structure Viewer 0.10.0，来源提交 `035ef0e7e648fa344d089de67acb99cbbf818f29`。迁入范围和许可证见 [来源记录](../src/chemistry_agent/web/structure/ORIGIN.md)。原扩展仓库未修改。

新增格式使用 ASE Atoms 作为内部表示；接口参照 [ASE 文件格式文档](https://ase.gitlab.io/ase/ase/io/io.html)。

测试命令：

```sh
python -m pytest
node --test tests/frontend.test.mjs tests/structure/*.cjs
python -m ruff check src tests
```

测试覆盖单位转换、非零盒原点、倾斜晶胞、ID 排序、元素映射、格式识别、无效/截断文件、上传上限和账号保护，并复用原扩展的核心前端回归测试。合成结构回归与浏览器验收不等于所有生产轨迹格式都已验证。

### 本地真实文件测试集

2026-10-31 从 cluster-b 完整下载 10 个代表文件（约 29.4 MiB），另生成 1 个 ASE `.traj`。原始文件远端/本地 SHA-256 一致；所有测试数据、来源路径、哈希与报告仅保存在被 Git 忽略的 `data/cluster-b-structures/`，不随代码上传。未读取 archive 目录，也未修改远端结构或任务。

| 格式 | 测试帧数 | 首帧原子数 |
| --- | --- | --- |
| GRO（2 个文件） | 各 1 | 368 / 10,425 |
| LAMMPS data（molecular） | 1 | 222 |
| LAMMPS dump | 2 | 1,096 |
| XDATCAR | 3,000 | 225 |
| CONTCAR | 1 | 225 |
| POSCAR | 1 | 226 |
| extxyz（2 个文件） | 21 / 200 | 222 / 224 |
| XYZ（ORCA 轨迹） | 23 | 9 |
| 生成的 ASE `.traj` | 21 | 222 |

重新执行整套本地文件验收只需：

```sh
python scripts/validate_structure_files.py
```

此入口先校验已下载原文件哈希，用 ASE 读取全部帧，再由浏览器使用的同一 JavaScript 解析器完整建立索引，比较首/中/末帧的元素、坐标、晶胞和 PBC。ASE `.traj` 由完整 21 帧 extxyz 生成，并逐帧比较转换前后的位置、晶胞、元素和 PBC。结果写入 `data/cluster-b-structures/validation/report.json`，生成轨迹及来源记录位于 `data/cluster-b-structures/generated/`。初次验证的最大坐标误差约为 `5×10⁻¹¹ Å`；这属于格式读取一致性检验，不是对计算本身的科学有效性判断。

上述入口需要先安装项目依赖，并已准备本地测试集；它不会联网、下载或提交远端任务。Node.js 需支持 `fs.openAsBlob`，本次使用 Node.js 26。
