import { t } from "./i18n.mjs";

export function fillModelOptions(select, models, current = "", { defaultLabel = "跟随默认模型", manual = false } = {}) {
  const options = [new Option(t(defaultLabel), "")];
  const seen = new Set();
  for (const model of models) {
    if (!model.id || seen.has(model.id)) continue;
    seen.add(model.id);
    options.push(new Option(model.name || model.id, model.id));
  }
  if (current && !seen.has(current)) options.push(new Option(current, current));
  if (manual) options.push(new Option(t("手动填写模型 ID"), "__manual__"));
  select.replaceChildren(...options);
  select.value = current;
}
