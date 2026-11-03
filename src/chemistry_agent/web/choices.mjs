// Keep the select as the single form control. Only the open menu is drawn here.
let current;
const menu = document.createElement("div");
menu.id = "chem-choice-menu";
menu.className = "choice-menu";
menu.dataset.noI18n = "";
menu.setAttribute("role", "listbox");
const hasPopover = typeof menu.showPopover === "function";
if (hasPopover) menu.setAttribute("popover", "manual");
const rows = () => Array.from(menu.querySelectorAll('[role="option"]:not([aria-disabled=true])'));
const selectFor = target => target instanceof HTMLSelectElement && !target.multiple &&
  target.size <= 1 && !target.matches(":disabled") ? target : null;

function close() {
  if (!current) return;
  const {select, observer, resize} = current;
  current = null;
  observer.disconnect();
  resize.disconnect();
  select.setAttribute("aria-expanded", "false");
  select.removeAttribute("aria-controls");
  select.removeAttribute("aria-activedescendant");
  if (hasPopover && menu.matches(":popover-open")) menu.hidePopover();
  menu.remove();
}

function highlight(row, scroll = true) {
  if (!row || !current) return;
  menu.querySelector("[data-active]")?.removeAttribute("data-active");
  row.dataset.active = "";
  current.select.setAttribute("aria-activedescendant", row.id);
  if (scroll) row.scrollIntoView({block:"nearest", inline:"nearest"});
}

function place() {
  if (!current) return;
  const select = current.select;
  if (!select.isConnected || select.matches(":disabled") || !select.getClientRects().length) {
    close(); return;
  }
  const rect = select.getBoundingClientRect();
  const pad = 6, gap = 4;
  const below = innerHeight - rect.bottom - pad - gap, above = rect.top - pad - gap;
  Object.assign(menu.style, {width:"max-content", maxWidth:`${innerWidth - 2 * pad}px`,
    minWidth:`${Math.min(innerWidth - 2 * pad, Math.max(rect.width, 128))}px`, maxHeight:"320px"});
  const up = below < menu.offsetHeight && above > below;
  menu.style.maxHeight = Math.max(32, Math.min(320, up ? above : below)) + "px";
  menu.style.left = Math.max(pad, Math.min(rect.left, innerWidth - pad - menu.offsetWidth)) + "px";
  menu.style.top = Math.max(pad, up ? rect.top - gap - menu.offsetHeight : rect.bottom + gap) + "px";
}

function draw() {
  const {select} = current;
  let group = null;
  const contents = [];
  for (const option of select.options) {
    if (option.hidden || option.parentElement.hidden) continue;
    if (option.parentElement !== group) {
      group = option.parentElement;
      if (group instanceof HTMLOptGroupElement) {
        const label = document.createElement("div");
        label.className = "choice-group";
        label.textContent = group.label;
        contents.push(label);
      }
    }
    const row = document.createElement("div");
    row.id = "chem-choice-" + option.index;
    row.setAttribute("role", "option");
    row.setAttribute("aria-selected", String(option.selected));
    row.setAttribute("aria-disabled", String(option.disabled || (group instanceof HTMLOptGroupElement && group.disabled)));
    row.dataset.index = option.index;
    row.textContent = option.dataset.menuLabel || option.label;
    contents.push(row);
  }
  menu.replaceChildren(...contents);
  place();
  if (current) highlight(rows().find(row => row.getAttribute("aria-selected") === "true") || rows()[0]);
}

function open(select) {
  close();
  if (!select.options.length) return;
  select.focus({preventScroll:true});
  const observer = new MutationObserver(() => { if (current?.select === select) draw(); });
  const resize = new ResizeObserver(place);
  current = {select, observer, resize, search:"", searchTime:0};
  const label = select.getAttribute("aria-label") || select.title ||
    Array.from(select.labels || [], el => el.innerText.split("\n")[0]).join(" ");
  menu.setAttribute("aria-label", label || (document.documentElement.lang === "en" ? "Options" : "选项"));
  (select.closest("dialog[open]") || document.fullscreenElement || document.body).append(menu);
  if (hasPopover) menu.showPopover();
  select.setAttribute("aria-expanded", "true");
  select.setAttribute("aria-controls", menu.id);
  draw();
  observer.observe(select, {childList:true, subtree:true, characterData:true,
    attributeFilter:["disabled", "label", "value", "selected", "hidden"]});
  resize.observe(select);
}

function commit(row) {
  if (!current || row?.getAttribute("aria-disabled") !== "false") return;
  const select = current.select, index = Number(row.dataset.index);
  close();
  select.focus({preventScroll:true});
  if (select.matches(":disabled") || select.selectedIndex === index) return;
  select.selectedIndex = index;
  select.dispatchEvent(new Event("input", {bubbles:true}));
  select.dispatchEvent(new Event("change", {bubbles:true}));
}

for (const name of ["pointerdown","mousedown"]) document.addEventListener(name, event => {
  if (event.button !== 0) return;
  const select = selectFor(event.target);
  if (select) { event.preventDefault(); select.focus({preventScroll:true}); }
  else if (current && !menu.contains(event.target)) close();
}, true);
document.addEventListener("click", event => {
  const select = selectFor(event.target);
  if (!select) return;
  event.preventDefault();
  if (current?.select === select) close(); else open(select);
}, true);
menu.addEventListener("pointermove", event => {
  if (event.pointerType === "touch") return;
  const row = event.target.closest('[role="option"][aria-disabled=false]');
  if (row && !row.hasAttribute("data-active")) highlight(row, false);
});
menu.addEventListener("click", event => commit(event.target.closest('[role="option"]')));

window.addEventListener("keydown", event => {
  const select = selectFor(event.target);
  if (!select || event.ctrlKey || event.metaKey || event.isComposing) return;
  const {key} = event;
  if (current && key === "Tab") { close(); return; }
  const letter = key.length === 1 && key !== " " && !event.altKey;
  if (!letter && !["ArrowDown","ArrowUp","Home","End","Enter"," ","Escape"].includes(key)) return;
  if (key === "Escape" && !current) return;
  event.preventDefault();
  event.stopImmediatePropagation();
  if (key === "Escape") { close(); return; }
  const wasOpen = current?.select === select;
  if (!wasOpen) open(select);
  if (!current) return;
  const options = rows(), active = options.find(row => row.hasAttribute("data-active"));
  if (key === "Enter" || key === " ") { if (wasOpen) commit(active); return; }
  if (letter) {
    const now = performance.now(), text = key.toLocaleLowerCase();
    current.search = now - current.searchTime < 700 ? current.search + text : text;
    current.searchTime = now;
    const prefix = [...current.search].every(ch => ch === text) ? text : current.search;
    const start = prefix.length === 1 ? options.indexOf(active) + 1 : 0;
    highlight([...options.slice(start), ...options.slice(0,start)].find(row => row.textContent.toLocaleLowerCase().startsWith(prefix)));
  } else {
    const index = key === "Home" ? 0 : key === "End" ? options.length - 1 :
      options.indexOf(active) + (key === "ArrowDown" ? 1 : -1);
    highlight(options[Math.max(0, Math.min(options.length - 1, index))]);
  }
}, true);
window.addEventListener("resize", place);
window.addEventListener("scroll", event => { if (current && !menu.contains(event.target)) place(); }, true);
document.addEventListener("focusin", event => {
  if (current && event.target !== current.select && !menu.contains(event.target)) close();
});
window.addEventListener("pagehide", close);
