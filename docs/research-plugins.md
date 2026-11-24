# Codex 科研插件原型

2026-11-23 起，两个插件的开发、构建和 bug 修复统一在独立私有仓库进行。安装新版本及配置本机服务见[插件仓库 README](https://github.com/liyang-tju/chem-research-plugins#readme)。下文记录 Chem-Agent 内置原型及既有部署，供核对旧版本使用。

这个原型把科研工具交给 Codex 使用，不启动第二个聊天 Agent、不读取模型凭据。现有 Chem Code 应用和数据仍保留。两个插件共用一个独立进程和隔离数据目录 `data/research-plugins/`。

## 当前行为

| 入口 | 已实现 | 当前边界 |
| --- | --- | --- |
| 计算作业监控 | 模拟作业、登记已有 Slurm 作业、状态观察、all/any 持久等待、事件读取/确认/取消 | 实际验收使用模拟作业；SSH 默认禁用；不提交或取消计算 |
| 研究方案 | 简单步骤与依赖、版本检查、看板/画布、步骤关联项目文件夹、目录分页导航 | 不读取计算参数，不解析结果文件；当前仅本地项目目录 |
| 文件打开 | MCP App 调用宿主 `openai/files/open`；宿主无此能力时显示准确路径 | 浏览器预览验证了路径回退；原生 Codex 文件打开尚待安装后验收 |
| 结构查看 | 使用已有 Atom Lens 工具及文件查看器 | 不复制或重建结构查看器 |
| 原对话自动唤醒 | cluster-a 常驻服务按已登记作业组唤醒对应聊天 | 汇总现有结果；无自动下游计算 |

方案侧面板只承担流程和文件导航。点击步骤切换到对应文件夹，可进入子目录、返回上级或返回流程。目录没有单独绑定时显示工作空间的项目根目录。没有计算参数、能量曲线和详细执行记录页面。

## 启动与试用

使用仓库已有 `.venv`，无需新的模型登录或 API key。脚本参数均位于各文件顶部的 Configuration 区域。

1. 构建两个插件包：

   ```sh
   .venv/bin/python scripts/build_research_plugins.py
   ```

   构建使用仓库的 `node_modules/.bin/esbuild`。缺少时仅需安装此构建工具（npm 包）：`npm install --no-save esbuild`。

2. 在一个终端启动共享服务：

   ```sh
   .venv/bin/python scripts/start_research_plugins.py
   ```

   默认监听 `127.0.0.1:29841`。`project_directory` 默认是本仓库，只有其内部的目录可登记为项目；`ssh_hosts` 默认为空。需要不同项目时编辑该路径。更改端口或数据目录后也要同步修改构建脚本并重新构建。

   服务部署在计算服务器时，可将 `local_host` 设为该服务器的标签（例如 `cluster-a`）。登记同名主机的作业会直接调用本机 `squeue`/`sacct`；`ssh_hosts` 中的其他主机仍通过 SSH 读取。两者默认均禁用，不会自动扫描或登记现有作业。

3. 在另一个终端运行完整模拟链路：

   ```sh
   .venv/bin/python scripts/demo_research_plugins.py
   ```

   这会通过两个真实 stdio MCP 子进程创建示例流程，登记模拟作业，关闭并重开订阅客户端，读取持久事件并确认，同时关联演示文件夹。演示工作空间 UUID 为 `11111111-1111-4111-8111-111111111111`。演示 thread UUID 是测试标识，未向真实对话发消息。

4. 可选浏览器预览：

   ```sh
   .venv/bin/python scripts/preview_research_plugins.py
   ```

   打开 <http://127.0.0.1:29842>。这是标明“宿主模拟”的本地验收页面，只能读取演示工作空间；不能据此认定 Codex 原生侧面板已验收。终端 Ctrl+C 停止。

仓库 marketplace 位于 `.agents/plugins/marketplace.json`，包含 `chem-job-monitor` 和 `chem-research`。两个插件以 `.codex-plugin/plugin.json` 为源清单，显式引用构建生成的 `.mcp.json`，并包含自包含 HTML 和 stdlib Python relay。当前 Codex 0.169.0 上已验证此兼容格式能加载工具；不要同时放置会覆盖它的根目录 `plugin.json`。cluster-a 已实际安装并完成原生工具/界面资源验证，详见下节；Mac 仍可通过本地预览检查 UI。

构建产物和机器路径保存在被 Git 忽略的插件文件中。插件安装会复制到缓存，所以更改连接配置或重新构建后需刷新安装。后台使用现有项目依赖（Python 3.11+）；stdlib relay 已在本机系统 Python 3.9 下运行通过。Linux 部署见下节；Windows 尚未验收。

## 工具协作方式

两个插件复用同一个稳定的 `workspace_id`。先 `workspace_open`，可同时传入已有的 `project_path`。`research_save_plan` 只公开精简的流程字段，内部转换成原有 PlanDraft，复用 DAG 校验、版本冲突检查和记录格式。

通过 `research_link_folder` 把 `plan_id`、`node_id` 绑定到项目内目录。文件列表每页最多 100 项，显示所有条目，包括 `archive`、隐藏项和符号链接。`research_read_folder` 的空路径打开步骤关联目录，之后使用项目根目录下的相对路径，可以返回项目根目录。项目内的符号链接可以打开；失效链接和指向项目外的链接仍显示，点击时提示原因。此原型只列目录，不读取文件正文。原生宿主打开文件前仍应用宿主自己的能力与权限。

`monitor_register_job` 返回平台 job UUID，它与 Slurm 调度器作业号不同。相同 workspace/key 只接受完全相同的登记请求；不同作业生命周期应使用新的 key。`monitor_register_wait` 以 workspace、thread、job 集合和 all/any 模式生成稳定的一次性等待。取消或确认后，同样登记会返回原等待，不会悄悄重新订阅。

等待和事件保存在 SQLite。观察状态更新与事件生成原子提交；读取不确认，处理后调用 `monitor_control_wait` 并提供准确 event UUID。多个消费者可能读到相同事件，执行方仍需按 event UUID 去重，不承诺外部副作用 exactly-once。这里的 thread UUID 只是调用方提供的路由标签，不能当作已验证的会话身份。

cluster-a 新登记默认使用自动汇总，明确要求手动查询时传 `delivery=pull`。`monitor_list_waits` 用于查看同一工作空间的多组等待和投递状态。没有配置 App Server 的部署继续使用查询模式。`COMPLETED` 只表示调度状态；退出码、科研步骤完成和科学收敛分别表达，SSH 错误或记账缺失保持 `UNKNOWN`。
