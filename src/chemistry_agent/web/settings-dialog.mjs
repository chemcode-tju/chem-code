// A normal settings URL remains usable as a separate page. Opening it from the
// workspace keeps the current chat, draft and scroll position in the document.
export function settingsHref(tab, session, host) {
  const params = new URLSearchParams();
  if (session) params.set('session', session);
  if (host && host !== 'local') params.set('host', host);
  return '/settings' + (params.size ? '?' + params : '') + '#' + tab;
}

export function initSettingsDialog({onOpen, onClose}) {
  const dialog = document.getElementById("workspace-settings");
  let frame;

  function show() {
    if (dialog.open) return;
    onOpen();
    frame = document.createElement("iframe");
    frame.title = document.documentElement.lang === "en" ? "Settings" : "设置";
    const options = new URLSearchParams({embed:"1", tab:location.hash.slice(1) || "general"});
    const source = new URLSearchParams(location.search);
    for (const key of ["session", "host"]) {
      if (source.has(key)) options.set(key, source.get(key));
    }
    frame.src = "/settings?" + options;
    dialog.replaceChildren(frame);
    dialog.showModal();
  }
  function dismiss() {
    if (history.state?.workspaceSettings) history.back();
    else dialog.close();
  }
  function open(tab, session, host) {
    if (dialog.open) return;
    history.pushState({workspaceSettings:true}, "", settingsHref(tab, session, host));
    show();
  }
  document.addEventListener("click", (event) => {
    const link = event.target.closest?.('a[href]');
    if (!link || event.defaultPrevented || event.button || event.metaKey || event.ctrlKey ||
        event.shiftKey || event.altKey || (link.target && link.target !== "_self")) return;
    if (link.origin !== location.origin || link.pathname !== "/settings") return;
    event.preventDefault();
    const target = new URL(link.href);
    const session = new URLSearchParams(location.search).get("session");
    if (session && !target.searchParams.has("session")) target.searchParams.set("session", session);
    open(target.hash.slice(1) || "general", target.searchParams.get("session"), target.searchParams.get("host"));
  });
  function restoreLinkedSettings() {
    if (location.pathname !== "/settings") return;
    // A copied link gets its own workspace history entry, just like a sidebar click.
    if (!history.state?.workspaceSettings) {
      const url = location.href;
      const search = new URLSearchParams(location.search);
      search.delete("host");
      history.replaceState(null, "", "/" + (search.size ? "?" + search : ""));
      history.pushState({workspaceSettings:true}, "", url);
    }
    show();
  }
  addEventListener("popstate", () => {
    if (history.state?.workspaceSettings && document.getElementById("login-screen").hidden) show();
    else if (dialog.open) dialog.close();
  });
  dialog.addEventListener("cancel", (event) => { event.preventDefault(); dismiss(); });
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dismiss();
  });
  dialog.addEventListener("close", () => {
    if (dialog.open) return;
    dialog.replaceChildren();
    frame = null;
    onClose();
  });
  addEventListener("message", (event) => {
    if (!frame || event.origin !== location.origin || event.source !== frame.contentWindow) return;
    if (event.data?.type === "chem:close-settings") dismiss();
    if (event.data?.type === "chem:settings-tab" && /^[a-z-]+$/.test(event.data.tab)) {
      history.replaceState({workspaceSettings:true}, "", "/settings" + location.search + "#" + event.data.tab);
    }
  });
  return {open, restoreLinkedSettings};
}
