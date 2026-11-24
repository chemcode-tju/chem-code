// At most one subscription per tab. Reconnection starts with a fresh full-text frame.
export function liveAnswer({ render, settle }) {
  let connection = null, key = "", text = "", steps = null, group = null;
  function close() { connection?.close(); connection = null; key = ""; text = ""; steps = null; group = null; }
  function follow(session, request) {
    const next = session && request ? session + "/" + request : "";
    if (next === key) { if (next) render(request, text, steps, group); return; }
    close();
    if (!next) return;
    key = next;
    const channel = new EventSource("/sessions/" + encodeURIComponent(session) + "/chat-requests/" + encodeURIComponent(request) + "/stream");
    connection = channel;
    channel.onopen = () => { if (connection === channel) { text = ""; steps = null; group = null; } };
    channel.addEventListener("draft", event => {
      if (connection !== channel) return;
      const frame = JSON.parse(event.data);
      text = frame.append ? text + frame.text : frame.text;
      if (Array.isArray(frame.steps)) steps = frame.steps;
      if (Array.isArray(frame.group_messages)) group = frame.group_messages;
      render(request, text, steps, group);
    });
    channel.addEventListener("settled", () => {
      if (connection === channel) { close(); settle(); }
    });
  }
  return { follow, close };
}
