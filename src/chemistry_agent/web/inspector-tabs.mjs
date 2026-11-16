export function restoreDockSelection(saved, available) {
  const stored = Array.isArray(saved?.tabs) ? saved.tabs : [saved?.tab];
  let tabs = [...new Set(stored.filter(name => available.has(name)))];
  // Version 1 opened a plan even when the user had never chosen a tab.
  if (saved?.version !== 2 && tabs.length === 1 && tabs[0] === "calculation") tabs = [];
  return {tabs, tab:tabs.includes(saved?.tab) ? saved.tab : tabs[0] || null};
}

/** Collapsing the dock preserves its tabs. Closing a tab removes that surface. */
export function initInspectorTabs(setVisible) {
  const tabs = new Map([...document.querySelectorAll("[data-inspector-tab]")].map(tab => [tab.dataset.inspectorTab, tab]));
  const add = document.getElementById("inspector-add");
  const frame = document.getElementById("structure-viewer-frame");
  const tablist = document.querySelector(".inspector-tabs");
  const launcher = document.getElementById("inspector-launcher");
  for (const [name, tab] of tabs) {
    tab.title = tab.textContent.trim();
    tab.setAttribute("aria-label", tab.textContent.trim());
    const choice = document.createElement("button");
    choice.type = "button";
    choice.dataset.launchTab = name;
    choice.append(...Array.from(tab.children, child => child.cloneNode(true)));
    launcher.append(choice);
  }
  let opened = [], active = null;

  function render() {
    launcher.hidden = opened.length > 0;
    tablist.hidden = opened.length === 0;
    tablist.dataset.count = String(opened.length);
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
      ? "Expand research panel"
      : "展开研究面板";
    toggle.setAttribute("aria-label", toggle.title);
  }

  function loadActive() {
    if (active === "structure" && !frame.hasAttribute("src")) frame.src = frame.dataset.src;
  }
  function focusActive() {
    (tabs.get(active) || launcher.firstElementChild).focus({preventScroll:true});
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
    const selection = restoreDockSelection(saved, tabs);
    opened = selection.tabs;
    active = selection.tab;
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
  launcher.addEventListener("click", event => {
    const choice = event.target.closest("[data-launch-tab]");
    if (choice) select(choice.dataset.launchTab);
  });
  document.querySelectorAll("[data-open-inspector]").forEach(button => {
    button.addEventListener("click", () => select(button.dataset.openInspector));
  });
  window.addEventListener("languagechange", render);
  restore();
  return {select, restore, loadActive, focusActive, snapshot:() => ({version:2, tabs:[...opened], tab:active})};
}
