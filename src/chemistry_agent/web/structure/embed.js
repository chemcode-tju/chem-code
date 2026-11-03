/* The inspector sends a structured-cloned File after the viewer is ready. */
(() => {
  const parentWindow = window.parent;
  const origin = window.location.origin;
  if (parentWindow === window || origin === "null" ||
      new URLSearchParams(window.location.search).get("embed") !== "1") return;

  document.documentElement.classList.add("embedded-viewer");
  function reply(type, fields = {}) {
    parentWindow.postMessage({type, ...fields}, origin);
  }

  window.addEventListener("message", async event => {
    if (event.source !== parentWindow || event.origin !== origin ||
        event.data?.type !== "chem:open-structure") return;

    const {file} = event.data;
    if (!(file instanceof File)) {
      reply("chem:viewer-error", {name: "", message: "请选择结构文件。"});
      return;
    }

    try {
      const opened = await openStructure([file], "auto");
      // A newer selection superseded this read, including a local file selection.
      if (!opened) return;
      if (opened.error) {
        reply("chem:viewer-error", {name: opened.name, message: opened.error});
      } else {
        reply("chem:viewer-loaded", {name: opened.name, message: ""});
      }
    } catch (error) {
      reply("chem:viewer-error", {name: file.name, message: error.message});
    }
  });
  reply("chem:viewer-ready");
})();
