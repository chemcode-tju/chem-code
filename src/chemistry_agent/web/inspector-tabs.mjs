import {fileIcon} from "./file-actions.mjs";
import {t} from "./i18n.mjs";

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
  const permanent = new Map(tabs);
  const documents = new Map();
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
    tablist.toggleAttribute("data-documents", documents.size > 0);
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
    add.disabled = [...permanent.keys()].every(name => opened.includes(name));
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
    discardDocument(name);
    if (active === name) active = opened[Math.min(index, opened.length - 1)] || null;
    render();
    setVisible(Boolean(active));
    if (active) {
      tabs.get(active).focus({preventScroll:true});
      tabs.get(active).scrollIntoView({block:"nearest", inline:"nearest"});
    }
    else document.getElementById("tools-toggle").focus({preventScroll:true});
  }
  function discardDocument(name) {
    const documentTab = documents.get(name);
    if (!documentTab) return;
    documentTab.dispose();
    documentTab.panel.remove();
    tabs.get(name).closest(".dock-tab").remove();
    documents.delete(name);
    tabs.delete(name);
  }
  function registerDocument(name, {label, title, panel, dispose}) {
    const chip = document.createElement("div");
    chip.className = "dock-tab dock-document-tab";
    chip.setAttribute("role", "presentation");
    chip.innerHTML = `<button type="button" role="tab">${fileIcon("structures")}<span data-no-i18n></span></button><button type="button" class="tab-close">${fileIcon("close")}</button>`;
    const [tab, close] = chip.children;
    tab.id = `inspector-tab-${name}`;
    tab.dataset.inspectorTab = name;
    tab.title = title;
    tab.setAttribute("aria-label", label);
    tab.querySelector("span").textContent = label;
    close.dataset.closeTab = name;
    close.title = t("关闭结构标签");
    close.setAttribute("aria-label", `${close.title} · ${label}`);
    panel.id = `inspector-panel-${name}`;
    panel.hidden = true;
    panel.setAttribute("role", "tabpanel");
    panel.setAttribute("aria-labelledby", tab.id);
    tab.setAttribute("aria-controls", panel.id);
    document.querySelector(".inspector-body").append(panel);
    tabs.set(name, tab);
    documents.set(name, {panel, dispose});
  }
  function clearDocuments() {
    for (const name of documents.keys()) discardDocument(name);
    opened = opened.filter(name => permanent.has(name));
    if (!tabs.has(active)) active = opened.at(-1) || null;
    render();
  }
  function restore(saved) {
    for (const name of documents.keys()) discardDocument(name);
    const selection = restoreDockSelection(saved, permanent);
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
  return {select, restore, loadActive, focusActive, registerDocument, clearDocuments, active:() => active,
    snapshot() {
      const names = opened.filter(name => permanent.has(name));
      return {version:2, tabs:names, tab:permanent.has(active) ? active : names.at(-1) || null};
    }};
}
