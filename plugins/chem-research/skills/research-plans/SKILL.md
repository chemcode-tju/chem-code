---
name: research-plans
description: Create and revise simple Chem research workflows, then link their steps to project directories for file navigation.
---

Use the same `workspace_id` as the monitoring plugin and call `workspace_open` first. `research_save_plan` accepts a title and simple steps (id, title, depends_on, optional purpose), backed by the existing Chem plan writer; revisions require `plan_id` and the current `expected_revision`. Saving a plan does not execute or approve it. Open `open_research_panel` to view the same records as a board or dependency canvas.

The panel is a workflow navigator. Keep nodes concise; do not build calculation-parameter readers or detailed result dashboards into it. Register an existing local `project_path` with `workspace_open` and link each step's directory through `research_link_folder`. `research_read_folder` lists every entry in pages of 100, including archive, hidden paths and symlinks. An empty path opens the step directory; subsequent paths are relative to the project root. Users can navigate back to that root. Symlinks within the project can be followed; broken links and links outside the project remain listed with an `open_error` and must not be opened. Clicking a file delegates its resolved path to the Codex host; this plugin never reads its contents. Remote directory browsing is not implemented in this prototype.

Link monitored jobs using both `plan_id` and `node_id`. Scheduler state never rewrites research-step completion. Saving a plan and linking directories do not modify research files.

For atomic structures and trajectories, reuse the user's installed Atom Lens `open_structure_path` tool when available. The panel uses the host's file-opening API, allowing installed file viewers to handle supported types. Do not recreate a viewer.
