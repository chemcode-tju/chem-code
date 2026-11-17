"""Build self-contained local plugin packages using the existing UI modules."""
# ruff: noqa: E402

# Configuration
data_directory = "data/research-plugins"
backend_url = "http://127.0.0.1:29841"

import base64
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    source = root / "src/chemistry_agent/research_plugins"
    esbuild = root / "node_modules/.bin/esbuild"
    if not esbuild.is_file():
        raise SystemExit("缺少 esbuild，请先安装 npm 构建依赖（npm install --no-save esbuild）。")
    with tempfile.TemporaryDirectory(prefix="chem-plugin-build-") as temporary:
        temp = Path(temporary)
        subprocess.run(
            [
                str(esbuild),
                str(source / "panel.mjs"),
                "--bundle",
                "--format=iife",
                f"--outfile={temp / 'panel.js'}",
                "--minify",
            ],
            check=True,
        )
        subprocess.run(
            [
                str(esbuild),
                str(source / "panel.css"),
                "--bundle",
                "--external:/assets/*",
                "--loader:.woff2=dataurl",
                "--loader:.woff=dataurl",
                f"--outfile={temp / 'panel.css'}",
                "--minify",
            ],
            check=True,
        )
        css = (temp / "panel.css").read_text()

        def inline_asset(match: re.Match) -> str:
            path = root / "src/chemistry_agent/web" / match[1]
            if not path.is_file():
                raise ValueError(f"Missing CSS asset: {path}")
            encoded = base64.b64encode(path.read_bytes()).decode()
            return f'url("data:image/svg+xml;base64,{encoded}")'

        css = re.sub(r"url\([\"\']?/assets/([^\)\"\']+)[\"\']?\)", inline_asset, css)
        js = (
            (temp / "panel.js")
            .read_text()
            .replace("</script", "<\\/script")
            .replace("/assets/ui-icons.svg", "")
        )
        icons = (
            (root / "src/chemistry_agent/web/ui-icons.svg")
            .read_text()
            .replace("<svg ", '<svg style="display:none" aria-hidden="true" ', 1)
        )
        for surface, package in (("monitor", "chem-job-monitor"), ("research", "chem-research")):
            target = root / "plugins" / package
            shutil.copyfile(source / "stdio.py", target / "server.py")
            connection = {
                "url": backend_url,
                "surface": surface,
                "token_file": str((root / data_directory / "access-token").resolve()),
            }
            (target / "connection.json").write_text(json.dumps(connection, indent=2) + "\n")
            mcp = {
                "mcpServers": {package: {"command": "python3", "args": ["server.py"], "cwd": "."}}
            }
            (target / ".mcp.json").write_text(json.dumps(mcp, indent=2) + "\n")
            mcp["$schema"] = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
            mcp["mcpServers"][package]["type"] = "stdio"
            (target / "mcp.json").write_text(json.dumps(mcp, indent=2) + "\n")
            manifest = json.loads((target / "plugin.json").read_text())
            legacy = {key: manifest[key] for key in ("name", "version", "description")}
            legacy.update(
                skills="./skills/",
                mcpServers="./.mcp.json",
                interface=manifest["extensions"]["com.openai"]["interface"],
            )
            (target / ".codex-plugin").mkdir(exist_ok=True)
            (target / ".codex-plugin/plugin.json").write_text(
                json.dumps(legacy, ensure_ascii=False, indent=2) + "\n"
            )
            html = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{legacy["interface"]["displayName"]}</title>
<style>{css}</style></head><body data-surface="{surface}">{icons}
<header><strong id="workspace-label">{legacy["interface"]["displayName"]}</strong>
<button id="refresh">刷新</button></header>
<details id="connection" open><summary>工作空间</summary>
<label for="workspace">工作空间 UUID</label><div class="connect-row">
<input id="workspace" autocomplete="off" spellcheck="false">
<button id="connect">打开</button></div></details>
<p id="status" role="status" aria-live="polite"></p>
<select id="plans" aria-label="研究方案" hidden></select>
<main id="content"></main><section id="detail" aria-label="步骤详情"></section>
<script>{js}</script></body></html>'''
            (target / "panel.html").write_text(html)
            print(f"Built {package}: {len(html.encode()) // 1024} KiB")


if __name__ == "__main__":
    main()
