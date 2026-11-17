---
name: monitor-jobs
description: Register existing computational jobs and durable completion waits in the Chem research backend. Use when monitoring simulations or recovering their events.
---

Use one stable `workspace_id` UUID across this plugin and the research plugin. Call `workspace_open` first. Register existing jobs with `monitor_register_job`; its simulated backend creates no process or scientific calculation. Reuse a job key only for the identical request, and use a new key for a new scheduler job lifetime.

Register a durable wait with the actual current Codex `thread_id`, the returned job UUIDs, and `all` or `any`. A wait is one-shot; identical registrations return the original wait, even after cancellation or acknowledgement. A job may be shared by several waits and chats.

This prototype uses pull delivery. `automatic_wakeup: unavailable` means the daemon has stored the wait but cannot wake an idle local chat. Keep waiting in the current turn with bounded `monitor_read_wait` calls when the user requested monitoring to completion. Do not say that ending the turn will resume automatically. Reconnect using the same wait UUID. Acknowledge the exact event UUID only after processing it; reads are non-destructive. Treat repeated event IDs as the same observation, not permission to repeat side effects. Cancel a wait with `monitor_control_wait`; this does not cancel the scheduler job.

Distinguish scheduler terminal state, exit code, parsed results, scientific convergence, and event acknowledgement. Unknown states or SSH errors never establish completion. The `thread_id` is a routing label supplied by the caller, not authenticated proof of chat ownership. No model login or secondary agent session is required.
