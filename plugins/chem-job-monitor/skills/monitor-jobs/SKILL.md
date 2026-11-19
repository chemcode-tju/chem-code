---
name: monitor-jobs
description: Track Slurm calculations and summarize results in the original chat. Use whenever submitting, adopting, or monitoring a calculation, and when handling a completion event.
---

Use one stable `workspace_id` UUID across this plugin and the research plugin. Call `workspace_open` first. Register existing jobs with `monitor_register_job`; its simulated backend creates no process or scientific calculation. Reuse a job key only for the identical request, and use a new key for a new scheduler job lifetime.

After submitting jobs, register them and their completion wait by default unless the user asks not to monitor or register them. Use the current chat UUID (`CODEX_THREAD_ID` from the current shell when available), returned job UUIDs, and `all` (whole group) or `any` (first terminal job) as requested. Use separate waits for independent groups. Each wait fires once, even after restart; identical registration returns the original wait. A job may be shared by several waits and chats.

On cluster-a, the default delivery is `codex`. Include a brief `summary` with the result paths and what to check. After `automatic_wakeup: enabled`, the background service waits and starts a summary turn when the original chat is free. End the current turn without polling. The summary is read-only and does not start downstream calculations. If the user wants manual-only monitoring, use `delivery: pull`; if they opt out of registration, skip it. `automatic_wakeup: unavailable` means manual lookup only.

Use `monitor_list_waits` to inspect groups and delivery status. After reporting results, acknowledge the exact event UUID with `monitor_control_wait`; reading is non-destructive. Cancel an individual wait with the same tool. Cancellation leaves the Slurm job running. An already started chat turn may need to be stopped in the chat.

Distinguish scheduler terminal state, exit code, parsed results, scientific convergence, and event acknowledgement. Unknown states or SSH errors never establish completion. The `thread_id` is a routing label supplied by the caller, not authenticated proof of chat ownership. No model login or secondary agent session is required.
