import { t } from "./i18n.mjs";
import { fillModelOptions } from "./models.mjs";

export function createChatModelPicker({ api, state, toast, updateControls }) {
  const select = document.getElementById("chat-model");
  const manual = document.getElementById("chat-model-manual");
  const input = document.getElementById("chat-model-id");
  let models = [], value = null, generation = 0, catalogVersion = 0, saving = false;
  function render() {
    fillModelOptions(select, models, value || "", { manual: state.health?.provider !== "chatgpt" });
    select.options[0].textContent = `${t("跟随默认模型")}${state.health?.model ? ` · ${state.health.model}` : ""}`;
    updateControls();
  }
  async function choose(next) {
    const previous = value, version = generation, sessionId = state.sessionId;
    saving = true; updateControls();
    try {
      if (sessionId) await api(`/sessions/${sessionId}/model`, { method: "PUT", body: JSON.stringify({ model: next }) });
      if (version !== generation) return;
      value = next; manual.hidden = true;
    } catch (error) {
      if (version === generation) { value = previous; toast(error.message); }
    } finally {
      saving = false; render();
    }
  }
  select.addEventListener("change", () => {
    if (select.value === "__manual__") { manual.hidden = false; input.value = value || ""; input.focus(); return; }
    choose(select.value || null);
  });
  document.getElementById("chat-model-apply").addEventListener("click", () => choose(input.value.trim() || null));
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); choose(input.value.trim() || null); }
    if (event.key === "Escape") { manual.hidden = true; render(); select.focus(); }
  });
  async function refresh() {
    const version = ++catalogVersion, account = state.accountVersion;
    document.getElementById("chat-model-refresh").disabled = true;
    try {
      const data = await api("/api/model/models");
      if (version !== catalogVersion || account !== state.accountVersion) return;
      models = data.models; render();
    } catch (error) {
      if (version === catalogVersion && account === state.accountVersion) { models = []; render(); toast(error.message); }
    } finally { document.getElementById("chat-model-refresh").disabled = false; }
  }
  document.getElementById("chat-model-refresh").addEventListener("click", refresh);
  window.addEventListener("languagechange", render);
  return {
    get value() { return value; },
    get saving() { return saving; },
    render, refresh,
    reset() { generation += 1; catalogVersion += 1; value = null; manual.hidden = true; models = []; render(); },
    newConversation() { generation += 1; value = null; manual.hidden = true; render(); },
    async loadSession(id) {
      const version = ++generation;
      const data = await api(`/sessions/${id}/model`);
      if (version !== generation) return;
      value = data.model; manual.hidden = true; render();
    },
    async saveNewSession(id, selected) {
      await api(`/sessions/${id}/model`, { method: "PUT", body: JSON.stringify({ model: selected }) });
    },
  };
}
