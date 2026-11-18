# 结构与轨迹查看器

在 Chem-Agent 首页点击“结构与轨迹”，或访问同一服务的 `/structures`。选择“本地文件”或直接拖入文件。可联合选择一条 LAMMPS 轨迹及对应的 data、输入和日志，也可选择一个 CP2K XYZ 和一个 CELL 文件。无需模型 Key。启用账号模式时，页面及转换接口均需要登录。

LAMMPS 优先使用引用当前轨迹的输入、其 `include` 文件及 `read_data` 指向的数据。同批选入的其他输入只有在关联明确时才参与解析。出现歧义时，查看器会列出冲突文件，已显示的结构仍可查看。CP2K 按 XYZ 注释中的 `i` 步号匹配 CELL，同一轨迹内各帧可使用不同晶胞。XYZ 未提供 PBC 时，按三维周期显示。

## 支持格式

| 文件 | 读取方式 | 说明 |
| --- | --- | --- |
| `.xyz`、`.extxyz` | 浏览器本地按需读取 | 多帧、变化原子数、extxyz 晶胞/PBC/原子 ID/质量 |
| POSCAR、CONTCAR、`.vasp`、`.poscar`、`.contcar` | 浏览器本地按需读取 | VASP 5/6，要求元素名称行，支持 Direct/Cartesian、Selective dynamics |
| XDATCAR、XDATCAR.1、`.xdatcar` | 浏览器本地按需读取 | 固定头部的多帧轨迹；重复头部/变胞拼接文件应先转换为 extxyz |
| ASE `.traj` | 浏览器本地按需读取 | ASE ULM 轨迹；不是任意后缀为 traj 的文本 |
| LAMMPS `.dump`、`.lammpstrj` | 浏览器本地按需读取 | atom/custom 文本 dump，正交/restricted/general triclinic 晶胞，Cartesian/scaled/unwrapped 坐标，按 atom ID 排序 |
| LAMMPS `.data`、`.lmp` | 当前服务转换为 extxyz | 从 Atoms 标题或配套输入识别 atomic/full/charge/molecular/bond/angle |
| GROMACS `.gro` | 浏览器本地按需读取 | 单帧或串接多帧，支持可变小数精度；nm 自动转换为 Å |
| `.pdb` | 当前服务转换为 extxyz | 结构与多 MODEL 坐标 |

其他文件名可在“文件格式与导入选项”中明确指定格式。暂不读取 GROMACS XTC/TRR/TPR、LAMMPS 二进制 dump、VASP 4 缺失元素名的结构。

### LAMMPS 的元素、单位和原点

点击“本地文件”时多选，或将一组文件一起拖入：

```text
LAMMPS/
  trajectory.lammpstrj
  system.data
  input.lammps        # 如果有
  log.lammps          # 如果有
```

数字 `type` 是力场类型，不是原子序数。查看器从 dump 的 `element` / `typelabel`、data 的 `Atom Type Labels` / `Masses` 注释、输入或日志中的 `labelmap atom` / `dump_modify element` 读取元素关系；也识别与类型总数一致的 `pair_coeff * *` 末尾元素列表。没有明确元素声明时，只接受与标准原子量相差不超过 0.03 u 的唯一匹配，并在界面标注“质量推断”。任意力场标签（例如 `C1`）不会直接被当作元素；同位素、虚拟位点、粗粒化质量可能无法自动识别。

`units`、`atom_style`、`boundary` 从所选配套文件解析；data 由 `write_data` 写出的标题中若有 units，也会识别。目前支持 `metal` / `real`（长度均为 Å）。缺少单位信息时明确提示按 Å 显示；其他单位制拒绝读取。元素或单位冲突时拒绝合并，并提示选择同一次计算的配套文件。无需用户手填映射。仅读取文本，不执行输入命令，不展开变量、不自动访问 `include` / `read_data` 路径；需要的配套文件应一起选择。

每次支持一条轨迹加多个配套文件，不自动拼接多条独立轨迹。配套文件合计限制 32 MiB；没有此上传限制的本地轨迹单独处理。dump 保留原子 ID 并排序，读取原始 wrapped/unwrapped 坐标列，不额外应用 image flags。晶胞原点整体平移到零。dump 优先使用 BOX BOUNDS 的边界标记；单独 data 若未提供 `boundary`，明确提示按三维周期显示。

实现参照 [LAMMPS dump 规范](https://docs.lammps.org/dump.html)、[read_data 规范](https://docs.lammps.org/read_data.html)、[type label 规范](https://docs.lammps.org/Howto_type_labels.html)、[倾斜晶胞规范](https://docs.lammps.org/Howto_triclinic.html) 与 [GROMACS GRO 格式规范](https://manual.gromacs.org/current/reference-manual/file-formats.html#gro)。

GRO 不提供独立元素字段，查看器根据原子名推断元素（与 ASE 采用相同的元素名称优先规则），请核对显示的元素。无法识别的虚拟位点/粗粒化原子会报错，而不是冒充真实元素。GRO 序号可能回绕，因此跨帧对应依据元素和顺序；原子排序变化的 GRO 不适合做质心漂移校正。GRO 盒尺寸也会从 nm 转为 Å。

## 使用

- 左键旋转、右键平移、滚轮缩放；Shift + 点击/拖动切换原子选择。
- 选择 2/3/4 个原子显示距离/角度/二面角，更多原子显示组成。切换选择不会重置镜头。
- 时间轴支持逐帧、跳帧和播放；在索引完成前帧数显示 `+`。
- Wrap 和质心漂移校正只改变显示，原文件不被修改。漂移校正要求连续、未折回坐标以及可靠的原子对应关系。
- 快照经浏览器下载，可选 PNG/JPEG/PDF、DPI、透明背景和裁剪。浏览器开始下载不等于已确认磁盘保存成功。

## 数据与资源边界

本地读取格式不会上传坐标。转换格式仅上传到当前 Chem-Agent 服务，在请求内转换，不保存原始文件或转换结果，不发送给 DeepSeek。远端部署时，这意味着转换格式会离开浏览器并传输到你的服务器。

本地读取保留原扩展的字节索引与 LRU 帧缓存（最多 16 帧、估算 64 MiB；单个大帧可能超过预算）。转换格式限上传 32 MiB、每帧 100,000 原子、10,000 帧、累计 1,000,000 原子、输出 64 MiB；转换完成后才显示第一帧。GRO 和文本 dump 通过 64 KiB 分块建立字节索引，首帧先显示，后台继续扫描；切帧时才解析目标帧，不受转换上传上限影响。

转换输出只保留查看所需的元素、位置、晶胞、PBC、质量和可用的 ID，不是完整的计算结果格式转换工具。力、能量、速度、拓扑等字段不作为分析数据输出。当前未接入势能计算器、几何优化、原生 macOS 保存助手或 FFmpeg 动画导出。

## 来源与验证

查看器复用 `liyang-tju/extxyz-extension` 的 Structure Viewer 0.10.0，来源提交 `035ef0e7e648fa344d089de67acb99cbbf818f29`。迁入范围和许可证见 [来源记录](../src/chemistry_agent/web/structure/ORIGIN.md)。原扩展仓库未修改。

服务端转换及独立对照验证使用 ASE Atoms；接口参照 [ASE 文件格式文档](https://ase.gitlab.io/ase/ase/io/io.html)。

测试命令：

```sh
python -m pytest
node --test tests/frontend.test.mjs tests/structure/*.cjs
python -m ruff check src tests
```

测试覆盖单位转换、非零盒原点、倾斜晶胞、ID 排序、元素映射、格式识别、无效/截断文件、上传上限和账号保护，并复用原扩展的核心前端回归测试。合成结构回归与浏览器验收不等于所有生产轨迹格式都已验证。

### 本地真实文件测试集

2026-10-31 从 cluster-b 完整下载代表文件，新增 500 帧 GRO 和 1501 帧 LAMMPS 及对应 data/日志；也补充 800 帧 extxyz 和 511 帧 XYZ。下载文件的远端/本地 SHA-256 一致。所有测试数据被 Git 忽略，不随代码上传，远端计算文件未修改。

结构文件直接放在 `data/structure-tests/` 的各用例目录，进入目录即可选取；来源清单、哈希、报告保存在 `data/cluster-b-structures/`。长轨迹用例：

| 用例目录 | 帧数 | 首帧原子数 |
| --- | --- | --- |
| `GROMACS/` | 500 | 10,182 |
| `LAMMPS/`（轨迹 + data + 日志一起选择） | 1,501 | 709 |
| `EXTXYZ_800/`（训练结构集合，不是连续 MD） | 800 | 225 |
| `XYZ_511/`（CP2K） | 511 | 345 |
| `XDATCAR/` | 3,000 | 225 |
| `ASE_TRAJ/`（由完整 XDATCAR 生成） | 3,000 | 225 |

另外保留 POSCAR、CONTCAR、两个单帧 GRO、molecular data、两帧 dump 与配套 data/输入、21/200 帧 extxyz、23 帧 ORCA XYZ，分别位于独立用例目录。短文件用于覆盖其他格式情况，不替代长轨迹验收。

重新执行整套本地文件验收只需：

```sh
python scripts/validate_structure_files.py
```

此入口先校验原文件和配套文件哈希，用 ASE 读取全部帧，再由浏览器使用的同一 JavaScript 解析器完整建立索引，比较首/中/末帧的元素、坐标、晶胞和 PBC。GRO 逐帧切分后交给 ASE 独立读取；LAMMPS 浏览器测试自动解析真实配套文件，不向前端传入手工元素映射。ASE `.traj` 由完整 3000 帧 XDATCAR 生成，并逐帧比较转换前后的位置、晶胞、元素和 PBC。

本地 Chrome 已实际联合选取 LAMMPS 轨迹/data/日志，确认自动类型映射并显示第 1501 帧；500 帧 GRO 已验证首帧、末帧和播放。

15 个真实结构用例均通过；新增长 GRO/dump 的首/中/末帧坐标与 ASE 一致（最大误差 0 Å），所有用例最大误差约 `5×10⁻¹¹ Å`。报告写入 `data/cluster-b-structures/validation/report.json`，生成轨迹位于 `data/structure-tests/ASE_TRAJ/`，来源记录保存在元数据目录。这是格式读取一致性检验，不代表对模拟结果的科学有效性判断。

上述入口需要先安装项目依赖，并已准备本地测试集；它不会联网、下载或提交远端任务。Node.js 需支持 `fs.openAsBlob`，本次使用 Node.js 26。
