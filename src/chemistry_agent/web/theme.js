/* Loaded before styles so navigation does not flash the wrong theme. */
(() => {
  const defaults = { mode: "system", fontSize: 14 };
  function normalize(value) {
    return { mode: ["light", "dark", "system"].includes(value?.mode) ? value.mode : defaults.mode,
      fontSize: [14, 16, 18, 20].includes(value?.fontSize) ? value.fontSize : defaults.fontSize };
  }
  function read() {
    try { return normalize(JSON.parse(localStorage.getItem("chemistry.appearance"))); }
    catch { return { ...defaults }; }
  }
  function apply(value) {
    const settings = normalize(value);
    const dark = settings.mode === "dark" || (settings.mode === "system" && matchMedia("(prefers-color-scheme: dark)").matches);
    document.documentElement.dataset.theme = dark ? "dark" : "light";
    const root = document.documentElement;
    root.style.setProperty("--reading-size", `${settings.fontSize}px`);
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
