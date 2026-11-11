# 计算进度事件接口

## 聊天回合的处理过程

聊天的实时输出使用 `/sessions/{session_id}/chat-requests/{request_id}/stream`，与下文的计算方案进度事件独立。`draft` 事件的 `text` / `append` 表示当前模型轮次的预览；可选的 `steps` 是已接受的中间说明及工具状态的完整快照，收到时替换，不追加。未带 `steps` 的文本增量保留已有步骤。重新连接的首帧包含完整快照。

步骤包括 `{type:"commentary", text}` 与 `{type:"tool", call_id, name, status}`。状态为 `running`、`completed`、`failed` 或 `uncertain`，只表示工具执行结果，不表示计算科学有效。公开快照不包含模型私有推理或原始工具参数。

运行中的“处理过程”默认展开；工具开始和下一轮模型输出不清空已接受的说明。最终回复完成后过程默认折叠，可重新展开查看，并在轮询时保留用户选择。刷新页面时，从请求列表的 `steps` 恢复未完成回合，从已保存的 assistant/tool 消息重建已完成回合。工具执行前保存说明及待核对状态；若进程在工具执行期间中断，继续沿用原来的不自动重放规则。

## 计算方案的进度事件

右侧步骤详情、左下消息窗口消费同一条会话事件流。用户发起的聊天照常回复。自动诊断、步骤结束、修复及 Agent 的阶段解读仅出现在进度流。整个方案结束并且可投递时，后端才向正式对话写入一条结果。打开面板、刷新页面或读取接口不会运行模型或调度计算。

## 读取

`GET /api/automation/chat/{session_id}/progress?after=0&limit=100`

| 参数 | 含义 |
| --- | --- |
| after | 排他整数游标，首次为 0，后续使用同一筛选条件的 next_cursor |
| limit | 1–200，默认 100 |
| plan_id | 可选 UUID，通用方案用方案 ID，旧 WorkChain、独立单作业及其重试链用最初根作业 UUID |
| node_id | 可选，通用方案用节点字符串，旧工作流用实际 AiiDA 节点 UUID |

```json
{
  "events": [
    {"seq":41,"plan_id":"153376a8-12dd-467d-beea-75e6b50ee703","node_id":"relax","kind":"step","status":"failed","message":"优化洁净表面","detail":{"attempt":0,"node_kind":"vasp","pk":116,"job_id":"e72c0653-c594-4fb1-b10a-dd7e4b160426","scheduler_job_id":"202001","error":"达到时限","structure_id":null,"completed_at":null},"created_at":"2026-11-03T01:05:01+00:00"},
    {"seq":45,"plan_id":"153376a8-12dd-467d-beea-75e6b50ee703","node_id":"relax","kind":"interpretation","status":"completed","message":"从原始输出确认本次为超时，已登记续算。","detail":{"run_id":"81e6801c-cdbe-4757-bbab-40f92e27a46c"},"created_at":"2026-11-03T01:06:01+00:00"}
  ],
  "next_cursor":45,
  "has_more":false
}
```

事件按 seq 升序。seq 只用于排序和去重，可以不连续。has_more 为真时用 next_cursor 继续取下一页；空页返回原 after。切换会话或筛选条件时单独保存游标。created_at 为事件写入时间；节点已有的 completed_at 在 detail 中。接口复用会话所有权和现有本机自动化访问检查。未拥有的会话返回 404，本机自动化不对共享认证部署开放。会话内查询其他会话的方案 ID 得到空页。

## 前端映射

| kind | 展示与导航 |
| --- | --- |
| submission | 用户点击确认后 API 仍返回提交回执。将回执展示在进度区，重复确认保留原作业。 |
| step | 通用方案的 message 为步骤标题，status 反映观测到的步骤/调度状态；旧工作流的 message 为状态说明，detail.label 为作业标签，detail.pk 为 AiiDA 编号。按 plan_id 和 node_id 关联，不按标题关联。 |
| diagnosis | 对应节点的自动诊断开始或诊断状态变化。 |
| repair | 通用方案节点登记修复，message 为原因，detail.attempt 从 1 起计。旧工作流的重试决定见 diagnosis。 |
| interpretation | Agent 回复原文位于 message；故障诊断的 detail.run_id 指向回合，阶段解读不插入聊天。单次诊断涵盖多个节点时，各节点均可查询，可用 run_id 在消息窗口合并。 |
| plan | 唯一的最终报告标记。node_id 为 null，detail.chat_event 等于正式 assistant 消息的 task_event。收到后重新读取会话并按 task_event 合并，无需将事件文本伪装成聊天回复。 |

诊断与按钮确认的运行记录可审计，run.context.automation_event 分别为 diagnosis、submission。UI 将聊天轮次与 runs 关联时排除这些运行。status=completed 的 interpretation 只代表解读已产出；科学有效性和频率、自由能等结果应读取对应步骤的实际产物。

## 投递与兼容

- 事件保存在本地 task-monitor.sqlite3 新表 progress。通用方案按节点及修复轮次去重，旧工作流按实际子作业及状态去重。等待配额、暂停、待修复确认或已自动提交重试都不会生成正式终态消息。
- 通用方案每个终态版本最多一条正式消息；旧工作流按最初根作业标识处理。用户正在聊天时，正式报告等待投递；进度仍可读取。重复轮询、服务重启后重放及重复确认保留事件 ID。
- 升级时保留历史聊天和原有 /events、通知与未读接口。调度器下次观察时可记录旧节点现存状态，历史上从未持久保存的瞬时事件不做重建。仅查询本接口不会触发回填、提交或恢复。
- 已停止方案在后台第一次扫描时，从本地 runtime 投影各节点及已保存尝试；不改变方案、不查询服务器，也不为回填的历史节点生成模型解读。
- 新观察到的 VASP 节点通过其既有完成条件后，将解读请求随完成检查点保存。SQLite 工作队列在调度之外调用原会话的模型，写入同一 plan_id/node_id 的 interpretation，detail.step_seq 指向对应的完成事件，并包含 attempt、job_id、pk、scheduler_job_id。建模和普通统计节点不入队。
- 解读状态为 completed 或 unavailable（模型不可用时只写一次提示）。已入队但尚未领取的解读遇到停算时记为 skipped。方案终态报告不等待逐节点解读，UI 可在收到终态后继续按游标读取。服务重启能续处理未完成的请求；已领取的请求在五分钟租约过期后可再次领取。若模型已应答、进程却在结果落盘前崩溃，恢复时可能再次调用模型，同一完成事件最多展示一条解读。
- 无新增第三方依赖。手动停算从原始用户回合回复，此接口的 plan 正式终态标记对应方案正常完成或失败。
