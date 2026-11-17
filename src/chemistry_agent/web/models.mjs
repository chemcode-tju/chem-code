import { t } from "./i18n.mjs";

export function fillModelOptions(select, models, current = "", { defaultLabel = "跟随默认模型", manual = false } = {}) {
  const options = [new Option(t(defaultLabel), "")];
  const seen = new Set();
  for (const model of models) {
    if (!model.id || seen.has(model.id)) continue;
    seen.add(model.id);
    const option = new Option(model.name || model.id, model.id);
    option.dataset.noI18n = '';
    options.push(option);
  }
  if (current && !seen.has(current)) {
    const option = new Option(current, current); option.dataset.noI18n = ''; options.push(option);
  }
  if (manual) options.push(new Option(t("手动填写模型 ID"), "__manual__"));
  select.replaceChildren(...options);
  select.value = current;
}
