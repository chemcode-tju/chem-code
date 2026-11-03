import { t } from "./i18n.mjs";
import { fillModelOptions } from "./models.mjs";

export function createChatModelPicker({ api, state, toast, updateControls }) {
  const select = document.getElementById("chat-model");
  const manual = document.getElementById("chat-model-manual");
  const input = document.getElementById("chat-model-id");
  let models = [], value = null, generation = 0, catalogVersion = 0, saving = false, refreshing = false;
  function render() {
    fillModelOptions(select, models, value || "", { manual: state.health?.provider !== "chatgpt" });
    const defaultModel = models.find(model => model.id === state.health?.model)?.name || state.health?.model;
    if (defaultModel) {
      select.options[0].textContent = defaultModel;
      select.options[0].dataset.menuLabel = `${t("跟随默认模型")} · ${defaultModel}`;
    }
    const actions = document.createElement("optgroup");
    actions.label = t("模型操作");
    const reload = new Option(t("刷新模型"), "__refresh_catalog__");
    reload.dataset.action = "refresh";
    reload.disabled = refreshing;
    const settings = new Option(t("模型设置"), "__open_settings__");
    settings.dataset.action = "settings";
    actions.append(reload, settings);
    select.append(actions);
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
    const action = select.selectedOptions[0]?.dataset.action;
    if (action) {
      select.value = value || "";
      if (action === "settings") document.getElementById("model-status").click();
      else void refresh();
      return;
    }
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
    refreshing = true; render();
    try {
      const data = await api("/api/model/models");
      if (version !== catalogVersion || account !== state.accountVersion) return;
      models = data.models;
    } catch (error) {
      if (version === catalogVersion && account === state.accountVersion) { models = []; toast(error.message); }
    } finally {
      if (version === catalogVersion && account === state.accountVersion) { refreshing = false; render(); }
    }
  }
  window.addEventListener("languagechange", render);
  return {
    get value() { return value; },
    get saving() { return saving; },
    render, refresh,
    reset() { generation += 1; catalogVersion += 1; refreshing = false; value = null; manual.hidden = true; models = []; render(); },
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
