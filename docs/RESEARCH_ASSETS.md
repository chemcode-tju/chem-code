# 科研资产

科研资产随 Chem-Agent 现有后端运行。清单进入当前数据库的研究记忆及版本历史，小文件按 SHA-256 保存到数据库旁的 `<数据库名>.files/research-assets/objects/`。备份时同时保留数据库和这个文件目录。不同 Agent 和节点通过同一个平台调用，节点访问沿用现有 SSH 配置。

## 登记旧计算和脚本

在现有 Chem-Agent 对话中，指定一个确认过的目录并请 Agent 调用 `asset_import`。外部 Agent 可使用 `/api/knowledge/mcp`，为某个项目登记时带上 `?project_id=<项目 ID>`。目录可以是 Agent 诞生前的普通计算目录，也可以是后来由计划运行使用的目录。登记只读原件。

| 用途 | `asset_import` 参数示例 |
| --- | --- |
| VASP 历史计算 | `{"host":"cluster-a","directory":"~/project/calc-01","kind":"vasp","title":"Cu 表面优化","files":["submit.sh"]}` |
| 分析脚本 | `{"host":"cluster-a","directory":"~/bin","kind":"script","files":["rdf.py"],"title":"轨迹 RDF","environment":"MDAnalysis、NumPy、Matplotlib" }` |
| 其他程序或自定义流程 | `{"host":"cluster-a","directory":"~/project/analysis","kind":"files","files":["config.yaml","analyze.py","results.csv"]}` |
| 组内收到的资产包 | `{"host":"local","directory":"/path/to/asset-package","kind":"bundle"}` |

`kind="vasp"` 自动选取可用的 POSCAR、CONTCAR、INCAR、KPOINTS、OSZICAR，保存 OUTCAR 的限量头尾及解析证据。可显式补充提交脚本、日志或其他过程文件。解析把最终离子步汇总的 TOTEN 标为 eV，电子收敛、离子收敛、文件结束标记分别记录，未能确认的字段保留未知。解析只依据选定文件的快照。脚本资产可补充依赖、曾经成功运行的输入约定和用途。单文件上限 8 MiB，每项上限 32 MiB，选文件时可保留大轨迹的来源说明。

同一项目重复导入相同内容返回原版本。同一来源的计算内容变化时追加版本。脚本和通用文件的集合参与来源识别。一项资产的多个文件由同一个固定版本锁定；原目录移走后，已冻结的小文件仍可取回。

## 检索、复用与课题组交换

1. `asset_search` 按名称、体系及用途找资产。VASP 可再用 `formula` 或 `functional` 精确筛选。项目外的本人记录可用 `scope="personal"` 搜索。
2. `asset_inspect` 接收 `asset_id` 和具体 `version`。核对能量口径、收敛证据、参数及文件用途。能量是否可比较仍取决于科学目的和完整输入条件。
3. `asset_checkout` 接收 `asset_id`、`version`、`host`、目标的现有 `directory`。校验后写入一个新的子目录，包含复用文件和 `chem-asset.json`，可从返回值在目标节点找到。执行脚本仍由当前研究流程决定。
4. 将固定的导出目录放到课题组已有的共享目录，或直接交给合作者。对方用 `kind="bundle"` 导入，在自己的项目可检索，并以同一份清单摘要追溯版本。签出清单保留原始主机与路径。当前共享是文件包交接，平台账户之间的在线授予和撤销留待需要时接入。
