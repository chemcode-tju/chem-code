/** Collapsing the dock preserves its tabs. Closing a tab removes that surface. */
export function initInspectorTabs(setVisible) {
  const tabs = new Map([...document.querySelectorAll("[data-inspector-tab]")].map(tab => [tab.dataset.inspectorTab, tab]));
  const add = document.getElementById("inspector-add");
  const frame = document.getElementById("structure-viewer-frame");
  const tablist = document.querySelector(".inspector-tabs");
  let opened = [], active = null;

  function render() {
    for (const name of opened) tablist.append(tabs.get(name).closest(".dock-tab"));
    for (const [name, tab] of tabs) {
      const selected = active === name, chip = tab.closest(".dock-tab");
      chip.hidden = !opened.includes(name);
      tab.setAttribute("aria-selected", String(selected));
      tab.tabIndex = selected ? 0 : -1;
      document.getElementById(tab.getAttribute("aria-controls")).hidden = !selected;
    }
    for (const option of add.options) option.hidden = !option.value || opened.includes(option.value);
    add.value = "";
    add.disabled = opened.length === tabs.size;
    document.getElementById("dock-count").textContent = String(opened.length);
    const toggle = document.getElementById("tools-toggle");
    toggle.title = document.documentElement.lang === "en"
      ? `Expand research panel (${opened.length} ${opened.length === 1 ? "tab" : "tabs"})`
      : `展开研究面板（${opened.length} 个标签页）`;
    toggle.setAttribute("aria-label", toggle.title);
  }

  function loadActive() {
    if (active === "structure" && !frame.hasAttribute("src")) frame.src = frame.dataset.src;
  }
  function ensureTab() {
    if (!active) { active = "calculation"; opened = [active]; render(); }
    loadActive();
  }
  function select(name) {
    if (!tabs.has(name)) return;
    if (!opened.includes(name)) opened.push(name);
    active = name;
    render();
    setVisible(true);
    const tab = tabs.get(name);
    tab.focus({preventScroll:true});
    tab.scrollIntoView({block:"nearest", inline:"nearest"});
  }
  function remove(name) {
    const index = opened.indexOf(name);
    if (index < 0) return;
    opened.splice(index, 1);
    if (active === name) active = opened[Math.min(index, opened.length - 1)] || null;
    render();
    setVisible(Boolean(active));
    if (active) tabs.get(active).focus({preventScroll:true});
    else document.getElementById("tools-toggle").focus({preventScroll:true});
  }
  function restore(saved) {
    const fallback = tabs.has(saved?.tab) ? saved.tab : "calculation";
    opened = Array.isArray(saved?.tabs) ? [...new Set(saved.tabs.filter(name => tabs.has(name)))] : [fallback];
    active = opened.includes(saved?.tab) ? saved.tab : opened[0] || null;
    render();
  }

  tablist.addEventListener("click", event => {
    const close = event.target.closest("[data-close-tab]");
    if (close) { remove(close.dataset.closeTab); return; }
    const tab = event.target.closest("[data-inspector-tab]");
    if (tab) select(tab.dataset.inspectorTab);
  });
  tablist.addEventListener("keydown", event => {
    const name = event.target.dataset.inspectorTab;
    if (!name) return;
    const index = opened.indexOf(name);
    const keys = {ArrowLeft:(index + opened.length - 1) % opened.length,
      ArrowRight:(index + 1) % opened.length, Home:0, End:opened.length - 1};
    if (event.key === "Delete") { event.preventDefault(); remove(name); }
    else if (Object.hasOwn(keys, event.key)) { event.preventDefault(); select(opened[keys[event.key]]); }
  });
  add.addEventListener("change", () => select(add.value));
  document.querySelectorAll("[data-open-inspector]").forEach(button => {
    button.addEventListener("click", () => select(button.dataset.openInspector));
  });
  window.addEventListener("languagechange", render);
  restore();
  return {select, restore, ensureTab, snapshot:() => ({tabs:[...opened], tab:active})};
}
