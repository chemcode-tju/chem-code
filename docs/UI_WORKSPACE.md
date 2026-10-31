# 工作空间 UI 与合并说明

## 分支基线

- UI 分支：`codex/ui-workspace`，起点为 `codex/provider-login` 的 `1995f05`。
- UI 实现提交：`cd6d6c0`。
- 该起点已包含 `codex/structure-viewer-integration` 的 `02b0486`，以及账号与模型配置功能。
- 未重写其他分支历史，未合并或推送其他功能分支。

## 功能与存储

- 紧凑的聊天/项目导航，项目可创建、重命名、折叠，并收纳多个对话。
- 项目内的「新对话」指定新聊天归属；顶部选择框移动现有对话；删除文件夹保留所有对话。
- 项目数据使用现有 SQLite 数据库的新增表 `projects` / `project_sessions`，服务启动时自动迁移。查询及写入均检查账户归属，原会话 API 保持兼容。
- 左下角账户入口链接到统一设置页，保留原有 ChatGPT 授权、API 地址、密钥、模型与连接测试逻辑。
- 白色/深夜/跟随系统与强调色保存在当前浏览器的 `chemistry.appearance`。不修改账户或模型凭据，不做跨设备同步。底色由模式决定，用户只能自定义强调色，按钮文字按亮度自动选择黑/白。
- 工具表单和工具记录默认折叠；生成结果后展开结果模块。
- 结构查看器的工具栏和设置面板使用统一主题，科学视口与导出图片仍使用查看器自身的背景设置。

## 模块边界

- `projects.py`：独立项目路由、数据表初始化；只在 `api.py` 和 `store.py` 各加入两行接入代码。
- `web/projects.mjs`：项目文件夹、项目对话与管理弹窗。
- `web/theme.js` / `web/theme.css`：共享外观初始化与颜色变量。
- `web/structure/shell-theme.css`：查看器外壳样式，不修改结构解析或三维渲染逻辑。

## 与 AiiDA 分支的关系

2026-10-31 对 `cd6d6c0` 与 `codex/aiida-automation` 的 `947afed` 执行了
`git merge-tree --write-tree --name-only` 模拟合并。没有修改工作区或分支指针。

两条分支的共同祖先是 `cacbd69`。AiiDA 分支独立引入了登录功能：
`1d92878` 与当前线上的 `1995f05` 补丁等价；`b3a1517` 与 `67a4a49`
目的相同，但补丁并不完全相同，因此不要再次盲目 cherry-pick 这些登录提交。

模拟合并存在以下 8 个冲突文件，后续合并时需显式处理：

| 文件 | 合并原则 |
| --- | --- |
| `README.md` | 保留新 UI 操作说明，加入 AiiDA 自动化说明 |
| `src/chemistry_agent/api.py` | 同时注册项目、结构、模型和 AiiDA 路由，保留双方生命周期初始化 |
| `src/chemistry_agent/web/app.mjs` | 保留项目导航、账户入口和 UI 状态管理，接入 AiiDA 计算事件及历史恢复 |
| `src/chemistry_agent/web/index.html` | 保留新布局，在紧凑导航/可折叠面板加入自动化入口和计算记录 |
| `src/chemistry_agent/web/settings.css` | 使用本分支的共享主题和设置样式 |
| `src/chemistry_agent/web/settings.html` | 保留账户/外观设置，逐项核对 AiiDA 分支新增的模型文案或字段 |
| `src/chemistry_agent/web/settings.mjs` | 保留原登录流程和新增外观功能，核对双方设置行为差异 |
| `src/chemistry_agent/web/styles.css` | 保留中性主题，将计算卡片样式改为共享颜色变量，避免重新引入绿色底色 |

建议先将此分支合入目标集成分支（已包含结构查看器与 provider-login），再合入 AiiDA
分支并解决上述冲突；也可在 AiiDA 集成分支合并本分支，但须按同样原则处理。
不要整体覆盖 `app.mjs` / `api.py`，也不要用全局 ours/theirs 跳过功能合并。

## 验证

- `python -m pytest -q`：108 项通过，包含项目持久化、移动、非破坏性删除及跨账户权限检查。
- `node --test tests/frontend.test.mjs tests/structure/test_*.cjs`：19 项通过。
- `python -m ruff check src tests` 与 `git diff --check`：通过。
- 浏览器检查：1280px 桌面、390px 窄屏、深夜主题与强调色持久化、项目创建/重命名、项目内发送消息、移出/移入项目、刷新恢复、真实 RDKit 乙醇属性计算、结构查看器加载；未发现控制台错误。
- 浏览器聊天使用临时数据库和模拟模型，没有调用真实模型或消耗额度。

上线前重启原服务以加载项目路由和数据库迁移。后续集成 AiiDA 时，还需运行该分支的
自动化测试，并复核计算预览、确认提交及历史记录的 UI 行为。
