/* Loaded before styles so navigation does not flash the wrong theme. */
(() => {
  const defaults = { mode: "light", accent: "#5865b5" };
  function normalize(value) {
    return { mode: ["light", "dark", "system"].includes(value?.mode) ? value.mode : defaults.mode,
      accent: /^#[0-9a-f]{6}$/i.test(value?.accent) ? value.accent : defaults.accent };
  }
  function read() {
    try { return normalize(JSON.parse(localStorage.getItem("chemistry.appearance"))); }
    catch { return { ...defaults }; }
  }
  function apply(value) {
    const settings = normalize(value);
    const dark = settings.mode === "dark" || (settings.mode === "system" && matchMedia("(prefers-color-scheme: dark)").matches);
    document.documentElement.dataset.theme = dark ? "dark" : "light";
    document.documentElement.style.setProperty("--accent", settings.accent);
    // Keep text legible even when the user picks a very pale accent.
    const channels = settings.accent.slice(1).match(/../g).map((c) => parseInt(c, 16) / 255)
      .map((c) => c <= .04045 ? c / 12.92 : ((c + .055) / 1.055) ** 2.4);
    const luminance = channels[0] * .2126 + channels[1] * .7152 + channels[2] * .0722;
    document.documentElement.style.setProperty("--on-accent", luminance > .179 ? "#111111" : "#ffffff");
    return settings;
  }
  globalThis.chemistryTheme = { read, apply, save(value) {
    const settings = apply(value);
    try { localStorage.setItem("chemistry.appearance", JSON.stringify(settings)); return true; }
    catch { return false; }
  } };
  apply(read());
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => apply(read()));
  addEventListener("storage", (event) => { if (event.key === "chemistry.appearance") apply(read()); });
})();
