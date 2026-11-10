const $ = id => document.getElementById(id);
const status = text => { $("status").textContent = text; };
const labels = { unverified: "未核验", observed: "已记录", converged: "收敛检查通过" };
let generation = 0;

async function api(url, options = {}) {
  const response = await fetch(url, { ...options, headers: { "Content-Type": "application/json" } });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.error?.message || `请求失败：${response.status}`);
  }
  return response.status === 204 ? null : response.json();
}
const base = () => `/api/sessions/${encodeURIComponent($("session").value)}/memories`;

async function search(event) {
  event?.preventDefault();
  const turn = ++generation;
  if (!$("session").value) return;
  const params = new URLSearchParams({ query: $("query").value, scope: $("scope").value });
  try {
    const result = await api(`${base()}?${params}`);
    if (turn !== generation) return;
    $("results").replaceChildren();
    $("detail").textContent = "选择记录查看正文、参数和证据。";
    for (const item of result.items) {
      const button = document.createElement("button");
      button.textContent = `${item.title}\n${labels[item.evidence.level] || item.evidence.level} · v${item.version}`;
      button.addEventListener("click", () => show(item.id, turn));
      $("results").append(button);
    }
    status(`找到 ${result.items.length} 条记录${result.truncated ? "，还有更多，请缩小关键词范围" : ""}。`);
  } catch (error) { status(error.message); }
}

async function show(identifier, turn) {
  const path = `${base()}/${encodeURIComponent(identifier)}`;
  try {
    const item = await api(path);
    if (turn !== generation) return;
    const title = document.createElement("h2"); title.textContent = item.title;
    const info = document.createElement("p");
    const updated = new Date(item.updated_at).toLocaleString("zh-CN", {hour12:false});
    info.textContent = `${labels[item.evidence.level]} · v${item.version} · ${updated}。收敛记录不代表科学精度已验证。`;
    const prose = document.createElement("pre"); prose.textContent = item.content;
    const evidence = document.createElement("pre");
    evidence.textContent = JSON.stringify({ systems: item.systems, tasks: item.tasks,
      parameters: item.parameters, applicability: item.applicability, evidence: item.evidence,
      source_session: item.session_id, source_key: item.source_key }, null, 2);
    const actions = document.createElement("div"); actions.className = "actions";
    const download = document.createElement("a"); download.href = `${path}/document.md`;
    download.textContent = "下载完整 Markdown";
    const versions = document.createElement("a"); versions.href = `${path}/versions`;
    versions.textContent = "版本记录"; versions.target = "_blank"; versions.rel = "noopener";
    const origin = document.createElement("a");
    origin.href = `/?session=${encodeURIComponent(item.session_id)}`;
    origin.textContent = "打开来源对话";
    const forget = document.createElement("button"); forget.textContent = "从检索中移除";
    forget.addEventListener("click", async () => {
      if (!confirm("移除后不再自动召回，后台同步也不会重新加入。原始计算及历史版本仍保留。")) return;
      try {
        await api(`${path}/forget`, { method: "POST", body: JSON.stringify({ expected_version: item.version }) });
        await search();
      } catch (error) { status(error.message); }
    });
    actions.append(download, versions, origin, forget);
    $("detail").replaceChildren(title, info, actions, prose, evidence);
  } catch (error) { status(error.message); }
}

$("search-form").addEventListener("submit", search);
$("session").addEventListener("change", search);
$("save-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (!$("session").value) return status("请先在研究工作区创建对话。");
  const split = id => $(id).value.split(/[,，]/).map(value => value.trim()).filter(Boolean);
  try {
    await api(base(), { method: "POST", body: JSON.stringify({ title: $("note-title").value,
      content: $("note-content").value, systems: split("note-systems"), tasks: split("note-tasks"),
      applicability: $("note-applicability").value }) });
    $("save-form").reset(); await search(); status("文档已保存，可在相关任务中检索。");
  } catch (error) { status(error.message); }
});

try {
  const sessions = await api("/sessions");
  for (const session of sessions) {
    const option = document.createElement("option"); option.value = session.id;
    option.textContent = session.title; $("session").append(option);
  }
  const requested = new URLSearchParams(location.search).get("session");
  if (sessions.some(session => session.id === requested)) $("session").value = requested;
  if (sessions.length) await search();
  else status("还没有对话。请先在研究工作区创建对话。");
} catch (error) { status(error.message); }
