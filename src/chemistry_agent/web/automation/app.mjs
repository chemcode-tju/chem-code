const $ = (id) => document.getElementById(id);
let state = { servers: [], codes: [] };
let preview = null;
let busy = false;
let revision = 0;
let polling = false;

function notice(message = "") { $("notice").textContent = message; $("notice").hidden = !message; }
async function api(path, body) {
  const response = await fetch(`/api/automation/${path}`, { method: body === undefined ? "GET" : "POST", headers: body === undefined ? {} : { "Content-Type": "application/json" }, body: body === undefined ? undefined : JSON.stringify(body) });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error?.message || JSON.stringify(data.detail || data));
  return data;
}
async function perform(action) {
  if (busy) return;
  busy = true; notice();
  document.querySelectorAll("button").forEach((button) => { button.disabled = true; });
  try { await action(); } catch (error) { notice(error.message); }
  finally { busy = false; document.querySelectorAll("button").forEach((button) => { button.disabled = false; }); }
}
function invalidate() { revision += 1; preview = null; $("preview-result").hidden = true; $("preview-empty").hidden = false; }
function selectedServer() { if (!$("server").value) throw new Error("请先选择服务器。"); return $("server").value; }
function updateCodes() {
  $("code").replaceChildren(new Option("请选择 VASP Code", ""));
  state.codes.filter((code) => code.server_id === $("server").value && code.plugin === "vasp.vasp").forEach((code) => $("code").add(new Option(code.label, code.id)));
}
async function refreshState() {
  state = await api("status");
  const previous = $("server").value;
  $("server").replaceChildren(new Option("请选择服务器", ""));
  state.servers.forEach((server) => $("server").add(new Option(`${server.label} · ${server.scheduler}`, server.id)));
  $("server").value = state.servers.some((server) => server.id === previous) ? previous : state.servers[0]?.id || "";
  updateCodes();
  $("system").textContent = state.daemon_running ? "AiiDA 已连接 · daemon 运行中" : "AiiDA 已连接 · daemon 未启动，暂不能提交";
}
async function refreshJobs() {
  const data = await api("jobs");
  $("jobs").replaceChildren();
  if (!data.jobs.length) { $("jobs").textContent = "暂无任务。"; return; }
  for (const job of data.jobs) {
    const card = document.createElement("article"); card.className = "job";
    const title = document.createElement("strong"); title.textContent = `AiiDA #${job.pk} · ${job.id.slice(0, 8)}`;
    const status = document.createElement("p"); status.className = "status";
    status.textContent = `${job.process_state}${job.paused ? " · 已暂停" : ""} · 调度器：${job.scheduler_state || "尚无记录"} · 作业号：${job.scheduler_job_id || "尚未分配"}`;
    const description = document.createElement("p"); description.textContent = job.process_status || job.exit_message || `退出码：${job.exit_status ?? "尚未退出"}`;
    const time = document.createElement("p"); time.textContent = `记录更新：${new Date(job.updated_at).toLocaleString()} · 查询：${new Date(job.checked_at).toLocaleTimeString()}`;
    const details = document.createElement("details"); const summary = document.createElement("summary"); summary.textContent = "查看任务与结果记录";
    const output = document.createElement("pre"); output.textContent = JSON.stringify(job, null, 2); details.append(summary, output);
    card.append(title, status, description, time, details); $("jobs").append(card);
  }
}
function showPreview(result) {
  preview = result; $("preview-empty").hidden = true; $("preview-result").hidden = false;
  $("preview-summary").textContent = `${result.structure.formula} · ${result.structure.atoms} 原子 · ${result.kind === "vasp" ? "VASP" : "联通测试（无科学计算）"} · ${result.resources.queue} · ${result.resources.wallclock_seconds} 秒`;
  $("preview-files").replaceChildren();
  for (const [name, content] of Object.entries(result.files)) {
    const details = document.createElement("details"); details.open = name === "_aiidasubmit.sh";
    const summary = document.createElement("summary"); summary.textContent = name;
    const code = document.createElement("pre"); code.textContent = content; details.append(summary, code); $("preview-files").append(details);
  }
}
$("refresh").onclick = () => perform(async () => { await refreshState(); await refreshJobs(); });
$("server").onchange = () => { invalidate(); updateCodes(); $("connection").hidden = true; };
$("job-form").addEventListener("input", invalidate);
$("kind").onchange = () => { $("vasp-options").hidden = $("kind").value !== "vasp"; $("vasp-options").open = true; invalidate(); };
$("test").onclick = () => perform(async () => { const result = await api(`servers/${selectedServer()}/test`, {}); $("connection").textContent = result.checks.map((check) => `${check.ok ? "✓" : "✕"} ${check.check}：${check.detail}`).join("\n"); $("connection").hidden = false; });
$("server-form").onsubmit = (event) => { event.preventDefault(); perform(async () => { const server = await api("servers", Object.fromEntries(new FormData(event.target))); await refreshState(); $("server").value = server.id; updateCodes(); invalidate(); notice("服务器配置已保存。请测试连接。"); }); };
$("code-form").onsubmit = (event) => { event.preventDefault(); perform(async () => { const code = await api("codes", { ...Object.fromEntries(new FormData(event.target)), server_id: selectedServer() }); await refreshState(); $("code").value = code.id; invalidate(); notice("VASP Code 已注册，环境脚本尚未执行。"); }); };
$("example").onclick = () => { $("format").value = "vasp"; $("structure").value = "Cu connectivity probe\n1.0\n3.6 0 0\n0 3.6 0\n0 0 3.6\nCu\n1\nDirect\n0 0 0\n"; invalidate(); };
$("structure-file").onchange = () => perform(async () => { const file = $("structure-file").files[0]; if (!file) return; if (file.size > 2_000_000) throw new Error("结构文件不能超过 2 MB。"); $("structure").value = await file.text(); if (/\.cif$/i.test(file.name)) $("format").value = "cif"; else if (/\.(xyz|extxyz)$/i.test(file.name)) $("format").value = "extxyz"; else $("format").value = "vasp"; invalidate(); });
$("job-form").onsubmit = (event) => { event.preventDefault(); perform(async () => {
  const body = { server_id: selectedServer(), kind: $("kind").value, structure: { content: $("structure").value, format: $("format").value }, resources: { queue: $("queue").value.trim(), nodes: Number($("nodes").value), ranks_per_node: Number($("ranks").value), wallclock_seconds: Number($("walltime").value), memory_mb: Number($("memory").value) } };
  if (body.kind === "vasp") Object.assign(body, { code_id: $("code").value || null, potential_family: $("family").value, potential_mapping: JSON.parse($("mapping").value), incar: JSON.parse($("incar").value), kmesh: $("kmesh").value.trim().split(/\s+/).map(Number) });
  if (body.kind === "vasp") Object.assign(body.resources, { gpus_per_node: Number($("gpus").value), gpu_type: $("gpu-type").value || null });
  const requestRevision = revision;
  const result = await api("previews", body);
  if (revision !== requestRevision) { notice("生成预览期间输入已修改，请重新生成预览。"); return; }
  showPreview(result);
}); };
$("submit").onclick = () => perform(async () => { if (!preview) return; const result = await api("submissions", { preview_id: preview.preview_id }); notice(`${result.reused ? "已找到原任务" : "已交给 AiiDA"}：#${result.job.pk}。调度器作业号将在实际提交后出现。`); await refreshJobs(); });
await perform(async () => { await refreshState(); await refreshJobs(); });
setInterval(() => { if (!busy && !polling && !document.hidden) { polling = true; refreshJobs().catch((error) => notice(error.message)).finally(() => { polling = false; }); } }, 15_000);
