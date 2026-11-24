import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const themeSource = readFileSync(new URL("../src/chemistry_agent/web/theme.js", import.meta.url), "utf8");
const glassCss = readFileSync(new URL("../src/chemistry_agent/web/solid-glass.css", import.meta.url), "utf8");
const chatWorkspaceCss = readFileSync(new URL("../src/chemistry_agent/web/chat-workspace.css", import.meta.url), "utf8");

function runThemeScript({ desktop = true, initialStorage = {} } = {}) {
  const posted = [];
  const docListeners = new Map();
  const winListeners = new Map();
  const storage = new Map(Object.entries(initialStorage));
  const styleProps = new Map();

  class FakeElement {
    constructor(tag, classes = [], attrs = {}) {
      this.tagName = tag.toUpperCase();
      this.classes = new Set(classes);
      this.attrs = attrs;
      this.parentElement = null;
    }
    matches(selector) {
      return selector.split(",").some((part) => {
        const s = part.trim();
        if (!s) return false;
        if (s.startsWith(".")) return this.classes.has(s.slice(1));
        if (s.startsWith("#")) return this.attrs.id === s.slice(1);
        const attrMatch = s.match(/^\[([^=\]]+)=['"]([^'"]+)['"]\]$/);
        if (attrMatch) return this.attrs[attrMatch[1]] === attrMatch[2];
        if (s.includes(" > ")) {
          const [parentSel, childSel] = s.split(" > ");
          return this.matches(childSel) && Boolean(this.parentElement?.matches(parentSel));
        }
        return this.tagName === s.toUpperCase();
      });
    }
    closest(selector) {
      let current = this;
      while (current) {
        if (current.matches(selector)) return current;
        current = current.parentElement;
      }
      return null;
    }
  }

  const documentElement = {
    lang: "zh-CN",
    dataset: {},
    style: { setProperty: (name, value) => styleProps.set(name, value) },
  };
  const context = {
    Element: FakeElement,
    Math,
    JSON,
    parseFloat,
    document: {
      documentElement,
      addEventListener(type, fn) {
        if (!docListeners.has(type)) docListeners.set(type, []);
        docListeners.get(type).push(fn);
      },
    },
    localStorage: {
      getItem: (key) => (storage.has(key) ? storage.get(key) : null),
      setItem: (key, value) => storage.set(key, String(value)),
    },
    matchMedia: () => ({ matches: false, addEventListener() {} }),
    getComputedStyle: () => ({ getPropertyValue: () => "44px" }),
    addEventListener(type, fn) {
      if (!winListeners.has(type)) winListeners.set(type, []);
      winListeners.get(type).push(fn);
    },
    webkit: desktop ? { messageHandlers: { chemDesktop: { postMessage: (msg) => posted.push({ ...msg }) } } } : undefined,
  };
  context.window = context;
  context.top = context;
  context.globalThis = context;
  vm.runInNewContext(themeSource, context);

  const dispatchDoc = (type, event) => {
    const full = { defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, ...event };
    for (const fn of docListeners.get(type) || []) fn(full);
    return full;
  };
  const dispatchWin = (type, event = {}) => {
    for (const fn of winListeners.get(type) || []) fn(event);
  };
  return { context, posted, FakeElement, dispatchDoc, dispatchWin, documentElement, styleProps };
}

test("theme.js syncs appearance mode to native macOS bridge and degrades cleanly in browsers", () => {
  const desktop = runThemeScript({ desktop: true });
  assert.deepEqual(desktop.posted, [{ action: "theme", mode: "system" }]);
  desktop.context.chemistryTheme.save({ mode: "dark", fontSize: 18 });
  assert.equal(desktop.documentElement.dataset.theme, "dark");
  assert.equal(desktop.styleProps.get("--reading-size"), "18px");
  assert.deepEqual(desktop.posted.at(-1), { action: "theme", mode: "dark" });

  const browser = runThemeScript({ desktop: false });
  assert.equal(browser.posted.length, 0);
  browser.context.chemistryTheme.save({ mode: "light", fontSize: 16 });
  assert.equal(browser.documentElement.dataset.theme, "light");
});

test("macOS titlebar drag only starts after pointer movement and ignores interactive controls", () => {
  const { posted, FakeElement, dispatchDoc, dispatchWin } = runThemeScript({ desktop: true });
  posted.length = 0;

  const toolbar = new FakeElement("header", ["queue-toolbar"]);
  const heading = new FakeElement("h1");
  heading.parentElement = toolbar;
  const button = new FakeElement("button");
  button.parentElement = toolbar;

  // Clicking and moving on a button inside the toolbar must not drag the window.
  dispatchDoc("mousedown", { button: 0, detail: 1, target: button, clientY: 20, screenX: 100, screenY: 20 });
  dispatchDoc("mousemove", { buttons: 1, screenX: 120, screenY: 20 });
  assert.deepEqual(posted, []);

  // Small jitter (< 3px) on the toolbar heading does not trigger window drag.
  const down = dispatchDoc("mousedown", { button: 0, detail: 1, target: heading, clientY: 20, screenX: 200, screenY: 20 });
  assert.equal(down.defaultPrevented, true);
  dispatchDoc("mousemove", { buttons: 1, screenX: 201, screenY: 21 });
  assert.deepEqual(posted, []);

  // Real drag (>= 3px) notifies native AppKit window drag once.
  dispatchDoc("mousemove", { buttons: 1, screenX: 210, screenY: 20 });
  dispatchDoc("mousemove", { buttons: 1, screenX: 230, screenY: 20 });
  assert.deepEqual(posted, [{ action: "drag" }]);
  dispatchWin("mouseup");

  // Double-clicking empty titlebar triggers macOS titlebarDoubleClick; on a button it does nothing.
  posted.length = 0;
  dispatchDoc("dblclick", { button: 0, target: button, clientY: 20 });
  assert.deepEqual(posted, []);
  dispatchDoc("dblclick", { button: 0, target: heading, clientY: 20 });
  assert.deepEqual(posted, [{ action: "titlebarDoubleClick" }]);

  // Top titlebar strip of the workspace rail is draggable, while below the titlebar height is not.
  posted.length = 0;
  const rail = new FakeElement("nav", ["workspace-rail"]);
  dispatchDoc("mousedown", { button: 0, detail: 1, target: rail, clientY: 80, screenX: 20, screenY: 80 });
  dispatchDoc("mousemove", { buttons: 1, screenX: 30, screenY: 80 });
  assert.deepEqual(posted, []);

  dispatchDoc("mousedown", { button: 0, detail: 1, target: rail, clientY: 16, screenX: 20, screenY: 16 });
  dispatchDoc("mousemove", { buttons: 1, screenX: 30, screenY: 16 });
  assert.deepEqual(posted, [{ action: "drag" }]);
});

test("macOS unified titlebar styles stay scoped to data-desktop=macos and reset in fullscreen", () => {
  assert.match(glassCss, /html\[data-desktop="macos"\]/);
  assert.match(glassCss, /html\[data-desktop="macos"\]\[data-desktop-fullscreen="1"\]/);
  assert.match(glassCss, /\.workspace-shell \.queue-toolbar/);
  assert.match(glassCss, /\.workspace-shell\.dock-max\.inspector-open \.inspector-strip/);
  assert.match(chatWorkspaceCss, /html\[data-desktop="macos"\]:not\(\[data-desktop-fullscreen="1"\]\) \.terminal-window > header/);
});
