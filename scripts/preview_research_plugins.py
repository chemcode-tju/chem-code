"""Browser QA host for the generated MCP App. Read-only, local demonstration only."""
# ruff: noqa: E402

# Configuration
preview_port = 29842
workspace_id = "11111111-1111-4111-8111-111111111111"

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "src"))
from chemistry_agent.research_plugins.stdio import Relay


class PreviewHandler(BaseHTTPRequestHandler):
    def respond(self, value: str, mime: str, code: int = 200) -> None:
        encoded = value.encode()
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def relay(self) -> Relay:
        query = parse_qs(urlsplit(self.path).query)
        package = "chem-job-monitor" if query.get("surface") == ["monitor"] else "chem-research"
        return Relay(root / "plugins" / package)

    def do_GET(self) -> None:
        if urlsplit(self.path).path == "/panel":
            self.respond((self.relay().root / "panel.html").read_text(), "text/html; charset=utf-8")
            return
        html = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>科研插件本地预览</title><style>
body{margin:0;font:14px system-ui;background:#f6f6f6}nav{padding:10px 16px;display:flex;gap:20px}
iframe{display:block;width:100%;height:calc(100dvh - 42px);border:0}a{color:inherit}
</style><nav><span>本地预览 · MCP 宿主模拟</span><a href="/">研究方案</a>
<a href="/?surface=monitor">作业监控</a></nav><iframe title="科研插件"></iframe><script>
const frame=document.querySelector('iframe');frame.src='/panel'+location.search;
addEventListener('message',async event=>{
 if(event.source!==frame.contentWindow)return;
 const m=event.data;if(m.jsonrpc!=='2.0')return;
 if(m.method==='ui/notifications/initialized'){
  frame.contentWindow.postMessage({jsonrpc:'2.0',method:'ui/notifications/tool-result',
   params:{structuredContent:{workspace_id:WORKSPACE}}},location.origin);return;}
 if(m.id==null)return;
 try{const result=m.method==='ui/initialize'?{protocolVersion:'2026-01-26',hostCapabilities:{},
  hostInfo:{name:'Local QA host',version:'1'},hostContext:{theme:'light'}}:
  await (await fetch('/rpc'+location.search,{method:'POST',
   headers:{'Content-Type':'application/json'},
   body:JSON.stringify(m)})).json();
  frame.contentWindow.postMessage({jsonrpc:'2.0',id:m.id,result},location.origin);
 }catch(error){frame.contentWindow.postMessage({jsonrpc:'2.0',id:m.id,error:{message:String(error)}},location.origin);}
});</script></html>""".replace("WORKSPACE", json.dumps(workspace_id))
        self.respond(html, "text/html; charset=utf-8")

    def do_POST(self) -> None:
        if self.headers.get("Origin") != f"http://127.0.0.1:{preview_port}":
            self.respond("{}", "application/json", 403)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length > 65536:
                raise ValueError("请求过大")
            message = json.loads(self.rfile.read(length))
            name = message.get("params", {}).get("name")
            args = message.get("params", {}).get("arguments", {})
            if (
                message.get("method") != "tools/call"
                or name
                not in {
                    "workspace_open",
                    "research_read_plan",
                    "monitor_list_jobs",
                    "research_read_folder",
                }
                or args.get("workspace_id") != workspace_id
            ):
                raise ValueError("预览仅允许读取演示工作空间")
            result = self.relay().dispatch(message["method"], message["params"])
        except (OSError, ValueError, KeyError) as error:
            result = {"isError": True, "content": [{"type": "text", "text": str(error)}]}
        self.respond(json.dumps(result), "application/json")


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", preview_port), PreviewHandler)
    print(f"Local QA preview: http://127.0.0.1:{preview_port}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
