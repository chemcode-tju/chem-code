// A normal settings URL remains usable as a separate page. Opening it from the
// workspace keeps the current chat, draft and scroll position in the document.
export function initSettingsDialog({onOpen, onClose}) {
  const dialog = document.getElementById("workspace-settings");
  let frame;

  function show(hash) {
    if (dialog.open) return;
    onOpen();
    frame = document.createElement("iframe");
    frame.title = document.documentElement.lang === "en" ? "Settings" : "设置";
    frame.src = "/settings?" + new URLSearchParams({embed:"1", tab:hash.slice(1) || "general"});
    dialog.replaceChildren(frame);
    dialog.showModal();
  }
  function dismiss() {
    if (history.state?.workspaceSettings) history.back();
    else dialog.close();
  }
  document.addEventListener("click", (event) => {
    const link = event.target.closest?.('a[href]');
    if (!link || event.defaultPrevented || event.button || event.metaKey || event.ctrlKey ||
        event.shiftKey || event.altKey || (link.target && link.target !== "_self")) return;
    if (link.origin !== location.origin || link.pathname !== "/settings") return;
    event.preventDefault();
    history.pushState({workspaceSettings:true}, "", link.pathname + link.hash);
    show(link.hash);
  });
  function restoreLinkedSettings() {
    if (location.pathname !== "/settings") return;
    // A copied link gets its own workspace history entry, just like a sidebar click.
    if (!history.state?.workspaceSettings) {
      const url = location.href;
      history.replaceState(null, "", "/" + location.search);
      history.pushState({workspaceSettings:true}, "", url);
    }
    show(location.hash);
  }
  addEventListener("popstate", () => {
    if (history.state?.workspaceSettings && document.getElementById("login-screen").hidden) show(location.hash);
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
      history.replaceState({workspaceSettings:true}, "", "/settings#" + event.data.tab);
    }
  });
  return restoreLinkedSettings;
}
