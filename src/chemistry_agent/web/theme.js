/* Loaded before styles so navigation does not flash the wrong theme. */
(() => {
  try { document.documentElement.lang = localStorage.getItem('chemistry.language') === 'en' ? 'en' : 'zh-CN'; } catch { /* Optional storage. */ }
  const defaults = { mode: "system", fontSize: 16 };
  function desktopBridge() {
    try { return window.webkit?.messageHandlers?.chemDesktop || null; }
    catch { return null; }
  }
  function postDesktop(payload) {
    try { desktopBridge()?.postMessage(payload); }
    catch { /* Optional native bridge. */ }
  }
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
    if (window === window.top) postDesktop({ action: "theme", mode: settings.mode });
    return settings;
  }
  globalThis.chemistryTheme = { read, apply, save(value) {
    const settings = apply(value);
    try { localStorage.setItem("chemistry.appearance", JSON.stringify(settings)); }
    catch { return false; }
    try { if (window !== window.top) window.top?.chemistryTheme?.apply?.(settings); }
    catch { /* Cross-origin parent. */ }
    return true;
  } };
  apply(read());
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => apply(read()));
  addEventListener("storage", (event) => { if (event.key === "chemistry.appearance") apply(read()); });

  if (window === window.top && desktopBridge()) {
    const dragSelectors = ".topbar, .queue-toolbar, .sidebar-header, .inspector-strip, .settings-header, .terminal-window > header, .auth-loading";
    const interactiveSelectors = "button, a, input, select, textarea, summary, label, [role='button'], [role='tab'], [role='option'], [contenteditable='true'], .workspace-target, .dock-tab, .dock-add, #dock-grip";
    const isTitlebarRegion = (target, clientY) => {
      if (!(target instanceof Element)) return false;
      if (target.closest(interactiveSelectors)) return false;
      if (target.closest(dragSelectors)) return true;
      const rail = target.closest(".workspace-rail");
      if (rail) {
        const height = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--desktop-titlebar-height")) || 44;
        return clientY <= height;
      }
      return false;
    };
    let pendingDrag = null;
    document.addEventListener("mousedown", (event) => {
      if (event.button !== 0 || event.detail !== 1) { pendingDrag = null; return; }
      if (!isTitlebarRegion(event.target, event.clientY)) { pendingDrag = null; return; }
      event.preventDefault();
      pendingDrag = { x: event.screenX, y: event.screenY };
    });
    document.addEventListener("mousemove", (event) => {
      if (!pendingDrag) return;
      if ((event.buttons & 1) === 0) { pendingDrag = null; return; }
      if (Math.hypot(event.screenX - pendingDrag.x, event.screenY - pendingDrag.y) < 3) return;
      pendingDrag = null;
      postDesktop({ action: "drag" });
    });
    const clearDrag = () => { pendingDrag = null; };
    addEventListener("mouseup", clearDrag);
    addEventListener("blur", clearDrag);
    document.addEventListener("dblclick", (event) => {
      if (event.button !== 0 || !isTitlebarRegion(event.target, event.clientY)) return;
      pendingDrag = null;
      event.preventDefault();
      postDesktop({ action: "titlebarDoubleClick" });
    });
  }
})();
