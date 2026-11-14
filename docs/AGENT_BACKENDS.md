# 可替换 Agent 与独立计算平台

## 源码调研（2026-11-13）

本次优先研究 DeepSeek 官方组织发布的 [dsh-superagent-prototype](https://github.com/deepseek-ai/dsh-superagent-prototype)，固定提交 `8c32a9dc5d72c033816c6a8110b37f40cf94fba4`。查询时约 22,816 stars，最后推送 2026-11-03。README 明确说明这是研究原型，不是正式产品，文章中的部分设计尚未实现。不要把文章愿景当作已有功能。

实际阅读：

- [core/ports.ts](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/8c32a9dc5d72c033816c6a8110b37f40cf94fba4/src/core/ports.ts)：Engine 只依赖 Repositories、Assessor、Executor、Outbox 等接口，执行器报告事实，任务领域核心保持唯一写入责任。
- [app/delivery.ts](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/8c32a9dc5d72c033816c6a8110b37f40cf94fba4/src/app/delivery.ts)：稳定消息 ID，检查历史与待投递收件箱，持久化后确认；区分 queue、steer 与不唤醒的 inject。
- [app/effects.ts](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/8c32a9dc5d72c033816c6a8110b37f40cf94fba4/src/app/effects.ts)：事务内记副作用，提交后执行；generation 防止旧执行覆盖新状态。
- [executor/dsh.ts](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/8c32a9dc5d72c033816c6a8110b37f40cf94fba4/src/executor/dsh.ts)：Ticket 的长期会话与目录独立于 Assignment；有限瞬态重试、启动恢复。
- [app/sessions.ts](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/8c32a9dc5d72c033816c6a8110b37f40cf94fba4/src/app/sessions.ts)：装饰会话的工具、提示词、技能；等待外部结果时结束回合。

对比阅读 [Vibe Kanban executors](https://github.com/BloopAI/vibe-kanban/tree/main/crates/executors/src/executors)（约 28,670 stars，11 月 12 日更新）：`StandardCodingAgentExecutor` 分离 spawn、spawn_follow_up、normalize_logs，Codex 直接连接 app-server。Zed（约 94,874 stars）统一内置/外部 Agent 会话的设计只作对照，不作为此次任务生命周期的主设计。DeepSeek Harness 主仓库约 279,989 stars，查询到 11 月 11 日更新。stars 为本次查询快照。

以上为架构参考，本实现没有复制上游源码。

## 在本项目中的对应关系

| DSH 概念 | 计算平台对应物 |
| --- | --- |
| Trigger | 用户持久消息、作业结束事件、Agent 注册的等待条件 |
| Ticket | 研究方案/研究步骤，生命周期可以跨越多个模型回合 |
| Assignment | 一次 Agent 执行尝试（run），失败不删除研究任务 |
| Executor / Sessions | BackendRouter + 独立 Agent 适配器 + agent_bindings |
| Effects / delivery receipt | 现有 chat_requests / agent_events，加 backend_deliveries |
| Outbox / Report | 持久回复、步骤报告、结果 artifacts、前端展示 |

Slurm 作业完成、Agent 回合结束、研究步骤完成分别记录。科研记忆继续属于平台；外部 Agent 的上下文、内部压缩与登录归外部 Agent 管理。没有把 GitHub 工单授权规则、Worker 工具白名单或 30 分钟后台作业终止策略移入科研平台。

## 已实现边界

- `backends/service.py`：运行接口与注册表；数据库保存默认后端及每个会话的绑定和配置快照。改默认只影响新对话；空闲会话可显式切换 Agent、模型和推理强度。
- `backends/delivery.py`：稳定投递 ID（用户请求 ID / 结束事件 ID），记录独立于某次 run。已完成结果可重用；投递开始后失联标记 uncertain，禁止自动重发。
- `backends/transport.py`：原生 Codex app-server stdio JSON-RPC；进程、请求、通知与断开处理。没有解析终端界面，没有复制 auth.json。
- `backends/codex.py`：thread/start、thread/resume、turn/start、turn/interrupt；动态平台工具、公开进度、结果与本轮用量。独立会话工作目录，原有 Codex 登录和配置由 Codex 自己使用。
- 原聊天队列和任务结束分发器调用同一 BackendRouter。任务监控仍不需要 LLM。动态工具沿用平台的会话权限和操作约束；文件/终端使用 Codex 原生能力。
- 外部会话的标题不额外调用内置模型；平台不再替 Codex 做上下文压缩。
- `/api/agents/settings`、`/api/agents/codex/probe` 和会话绑定查询仅支持本地单用户访问。设置页合并为“Agent 与模型”，Chem Agent 和 Codex 分别展开；API Key/ChatGPT 授权继续属于内置模型服务。

## 恢复与权限

App-server 的关联消息 ID 只用于关联，不宣称它对任意工具或终端副作用提供 exactly-once。平台先保存投递回执再发送；发送后不明的情况保守阻断自动重发。后续新消息可以在原 Codex thread 中核对现场。完成结果先保存在投递回执，再写入平台聊天。

Codex 权限映射：

| 选项 | sandbox | approvalPolicy | approvalsReviewer |
| --- | --- | --- | --- |
| 批准 | workspace-write | on-request | user |
| 帮我批准 | workspace-write | on-request | auto_review |
| Full access | danger-full-access | never | user |

人工批准通过会话内操作卡提交单次回执。命令、文件修改和额外权限请求均可回传；请求绑定 session/run，重复回答被拒绝，取消或重启后过期，不自动重放。原生自动审批由 Codex 实现，平台工具仍遵守宿主授权。在“批准”模式，平台有上下文的工具在执行前也需人工批准；纯分子描述符和 ask_user_question 不重复提问。

Chem Agent 支持“批准”与“Full access”（已注册平台工具按现有授权执行）；它没有操作系统沙箱，不能把权限按钮当作 OS 隔离承诺。Chem Agent 尚无独立自动审批器，UI 禁用“帮我批准”，接口也拒绝伪装生效。安全设置不替代科学方案确认及服务器授权。

模型目录保留实际支持的推理等级；Codex 在 turn/start 传 effort，Chem Agent 的 ChatGPT Responses/兼容 CLI 传 reasoning effort，DeepSeek 提供思考开关，未声明推理能力的兼容 API 不显示虚构档位。

## 扩展边界与尚未实现

当前真正接通的是内置 Agent、Codex 和 Antigravity（官方 ACP v1），尚未接入 DSH。未来 DSH 可实现同一个运行适配器，或通过 ACP / DSH SDK 接入，不需迁移作业与科研记忆。不要把“预留接口”展示成“已支持 DSH”。

运行或已排队回合不允许中途切换执行配置；空闲会话可切换，切换消费者时启动新的原生会话并以平台历史文件交接，避免复用过时原生上下文。目前不迁移 Codex Desktop 的任意既有对话、不实现 DSH Router/Worker 多 Agent 协作、不对任意终端命令做自动副作用对账。后续完善方向是带版本的交接快照、显式人机权限交互，以及 accepted/read/completed 三阶段消息回执。任务事件不会自动切换到其他后端以绕过配额失败。

## 验收

真实本机 codex-cli 0.164.0：app-server 握手、读取登录状态、读取模型目录通过。小型验收调用一次平台 molecular_properties(CO)，返回 CH₄O；断开连接后恢复同一个 thread，再问上一轮分子式，仍返回 CH₄O，没有重复调用工具。未提交任何科学计算。

自动化测试覆盖普通聊天与结束事件使用同一后端、工具与记忆接口桥接、旧会话保持原后端、服务重启后续聊、稳定消息去重、失联结果不明禁止重发、本地访问边界。

最终验证：81 项相关后端测试通过；注册表补充改动后 9 项针对性测试通过；38 项前端测试通过；Ruff、JavaScript 语法与 git diff --check 通过。设置页人工操作验证了“本地 Codex → 检测连接与登录 → 保存设置”。默认使用本机已有登录，不改变旧会话绑定。验收服务仍为 localhost:8768。


## 2026-11-13 UI 与执行配置验收

与“重做界面并支持主题切换”对话只读协调，使用现有主题变量、折叠行和紧凑菜单。
聊天三级菜单只在最终选项提交完整组合；模型按钮按内容宽度显示，窄屏提供返回导航。
旧 #model/#agent 链接仍可打开合并设置。默认设置与会话选择分离；批准回执不会伪装成用户聊天消息。

针对性测试覆盖会话配置持久化、原生审批策略/effort 传递、忙碌拒绝切换、人工批准回传、拒绝无工具副作用、重启过期和重复回执拒绝。网页实际检查了合并设置、模型目录、三级菜单、窄屏返回、权限项。验收没有提交计算。


## Antigravity 原生后端（2026-11-13）

官方 [ACP 接口](https://antigravity.google/docs/ide/extensions/acp/)通过 `agy acp` 启动。
CLI 使用自身账户认证；平台不复制登录凭据、不冒充其他 ACP 客户端。
设置页第三行提供程序路径、实际账户模型和模型支持的思考等级。
聊天沿用 Agent → 模型 → 推理强度菜单。

- `acp_transport.py`：ACP v1 JSON-RPC 传输、超时、断连和进程清理。
- `antigravity.py`：创建/恢复原生会话、配置模型、流式回复和公开工具状态。
- `mcp_bridge.py`：按回合建立临时 MCP 连接，回调宿主 ToolRegistry，身份绑定平台 session/run。
  Unix socket 为本机私有、随机令牌验证、回合结束后失效；科研记忆、方案和监控仍属于平台。
- “批准”对应原生 default 模式；“帮我批准”对应原生 auto；Full access 才使用
  `--dangerously-skip-permissions`。原生审批请求在原对话显示，默认只允许/拒绝一次。
- 每轮结束关闭 ACP 子进程；下一轮使用同一个原生 sessionId 恢复，并重建工具桥。
  历史回放不再次写入当前回复；不确定的回合沿用投递账本，禁止自动重发。
- 文件沿用工作区自动发布，提供在线结构查看及下载。Antigravity 自己管理上下文；
  ACP 的上下文占用不会冒充计费 token 用量。

依赖已安装的官方 CLI；无 CLI 时提示安装，登录失效时提示在本机终端运行 `agy`。
这不是 Antigravity 桌面窗口控制，也不会接管桌面中任意已有对话。
原生终端权限由 Antigravity 管理，平台工具继续遵守原有会话与提交授权。

## Native context and compaction

`GET /sessions/{id}/context` returns persisted native measurements for external backends:
`used_tokens`, `capacity_tokens`, `updated_at`, optional `compaction`, `manual_supported`,
`busy`, and the most recent maintenance `operation`. Missing numbers remain null/absent.
The UI hides missing rows and hides the entire entry if neither measurements nor actions
are available. Cumulative/billed usage is never used to compute context occupancy.

- Codex: `thread/tokenUsage/updated.last.totalTokens` and `modelContextWindow`;
  automatic and manual `contextCompaction` item lifecycle events.
- Antigravity: ACP `usage_update.used/size`; manual `/compact` only when the current
  session advertises it in `available_commands_update`. The installed ACP version does
  not emit a separate automatic-compaction lifecycle; no status is inferred from a
  decrease in tokens. Manual completion follows the native command's `end_turn`.
- `POST /sessions/{id}/context/compact?operation_id=<uuid>` claims the same session lease
  as chat, persists a receipt and returns 202. Duplicate IDs return that receipt; a
  different request while busy returns 409. Queued user messages keep their priority.
- `POST /sessions/{id}/context/refresh?operation_id=<uuid>` resumes the existing native
  session to collect usage/capabilities, without a model prompt. ACP notifications can
  arrive after the load response. Page polling reads platform SQLite only.
- Maintenance survives page navigation. Backend restart marks unfinished operations
  interrupted, without replay. The next native turn resumes the same thread/session.
  Maintenance emits no synthetic user/assistant messages and cannot submit research jobs.
- Compaction is entirely native: the platform stores observations/receipts, never a
  substitute summary. A missing post-compaction usage measurement stays hidden, and a
  later ordinary chat turn is not mislabeled as a compaction saving.

References: [Codex app-server](https://developers.openai.com/codex/app-server),
[Antigravity ACP](https://antigravity.google/docs/ide/extensions/acp/).

## 2026-11-14：隔离桌面会话与恢复失败事件

发现平台默认继承桌面的 `~/.codex`。桌面载入同一 thread 后持有 writer，平台 `thread/resume` 返回 `already has an active writer`，尚未开始模型回合。仅隐藏列表无法解决共享运行状态的问题。

现在每个平台会话使用 `data/.../codex-state/<session>/` 作为子进程专用 Codex home；宿主环境不变，不共享 sessions、SQLite、锁、缓存或日志。每个 home 有进程级写锁，其他会话可并行。现有 auth.json 以本地引用复用，配置文件首次复制，规则与技能保持可用；不读取或打印登录令牌。若本机只用系统钥匙串而没有 auth.json，需要在该独立环境完成登录，不会伪报已认证。

旧会话仅迁移明确绑定的单个完整 rollout：核对无未结束回合、复制前后源文件没有变化，保留原 thread ID、原生上下文和动态工具；之后只写平台副本，不再覆盖或同步桌面副本。不存在可恢复记录或仍有活跃回合时明确阻断，不悄悄开空会话。不调用模型进行迁移。平台既有会话/方案/作业 ID 均不变。

投递区分 `not_sent`、`started`、`completed`、`uncertain`。只有适配器确认还没调用 `turn/start` 的错误可留下 `not_sent`，允许同一消息 ID 重试。开始发送后的失联仍保留 `uncertain`，不能盲目重放。失败回合更新会话版本、保留自动唤醒标签与可读错误，界面停止计时并显示失败；恢复回复在时间顺序中位于失败尝试之后。

研究参考（原创实现，不复制上游代码）：

- [OpenAI App Server](https://learn.chatgpt.com/docs/app-server)：`thread/read` 与恢复/执行接口分离、持久会话生命周期。
- [codex-plugin-cc #382](https://github.com/openai/codex-plugin-cc/issues/382)：仍为开放问题，其中提出独立 home 和登录引用的方案；不能表述成已经合入的官方修复。
- [Vibe Kanban Codex executor](https://github.com/BloopAI/vibe-kanban/blob/main/crates/executors/src/executors/codex.rs)：Codex home 与 executor 生命周期分层，支持自定义 home；本次未据此声称它默认按会话隔离。
- [OpenClaw #73910](https://github.com/openclaw/openclaw/issues/73910)：隔离 home 后缺少 auth bridge 的故障报告，提醒隔离状态与复用登录必须一起验证。

## 统一原生会话运行环境

`backends/runtime.py` 的 `NativeSessions`、`SessionLease` 和 `ManagedTransport`
由 Codex app-server 与 Antigravity ACP 共用。聊天、后台事件、设置探测、上下文刷新和压缩
均通过对应会话的运行环境；页面读取已有数据不启动原生 Agent。

- **归属**：每个后端/平台会话一个私有数据目录，原生会话 ID 持久绑定；无法恢复时不悄悄
  创建空会话。Codex 沿用 `codex-state/<session>`；Antigravity 使用
  `antigravity-state/<session>` 作为 `--gemini-dir`，相对的 `--app-data-dir antigravity-cli`。
- **进程锁**：在准备目录、迁移历史或启动进程之前取得 `flock`，锁描述符传给原生子进程。
  平台崩溃时，仍在工作的子进程继续持锁；不因超时就认定它死亡或启动替代者。正常退出或
  中断清理整个进程组后释放；启动失败也释放。不会停止其他会话或远端 Slurm 作业。
- **迁移**：Codex 仍只导入明确绑定且已结束的 rollout；Antigravity 使用 SQLite backup
  读取 WAL 一致快照，仅迁移绑定会话及其 brain/annotation。迁移前后检查文件占用和变动；
  已存在的私有历史绝不被旧副本覆盖。保留原数据，不复制全局会话索引。
- **能力**：保留用户工具配置、技能和原先权限模式。Antigravity 复用原生系统凭据存储，
  Codex 保留原登录引用；私有状态不意味着独立账号或独立额度。没有新增计算并行数上限。
- **交付**：两种适配器共用 `delivery_attempt`；native prompt 调用前失败记 `not_sent`，
  允许原消息/事件重试并恢复 checkpoint 的可重试阶段。发送后失联记 `uncertain`；完成后
  重复投递返回原回执。只有确定未发送的临时占用错误参与原有、有界的事件重试。
- **压缩**：沿用原生能力和操作回执，复用同一会话锁。重复 operation ID 不再次压缩；
  中断不会伪装成成功，也不会把科研步骤作为压缩重放。

这些机制防止平台重放不确定投递；任意 Agent 终端命令的外部副作用仍需按真实作业记录
对账，不能把事件去重宣称为任意 shell 命令的 exactly-once。原生后台仍活跃时需等待或
明确结束该后台，不能靠删除锁文件强行接管。

参考 Lody 的 [创建归属](https://github.com/LodyAI/Lody/blob/36ee6a115fcb6468038d15a06d0b9016d447cb5f/apps/cli/src/session/session-manager.ts#L504-L541)、
[生命周期清理](https://github.com/LodyAI/Lody/blob/36ee6a115fcb6468038d15a06d0b9016d447cb5f/apps/cli/src/lib/loro/session-active-presence.ts#L301-L352)
和 [ACP 启动拥堵记录](https://github.com/LodyAI/Lody/blob/36ee6a115fcb6468038d15a06d0b9016d447cb5f/apps/cli/src/agent/acp-session-start-gate.ts#L3-L12)。
本实现原创，未复制上游代码，也没有把 Lody 的启动并发阈值套到计算队列。
