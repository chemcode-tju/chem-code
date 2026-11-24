# 科研资产目录 Demo

在 Chem-Agent 仓库根目录，用现有 Python 环境运行。

```sh
.venv/bin/python examples/research_assets/demo.py
```

Demo 创建临时 SQLite，启动实际的本机 MCP HTTP 服务，再用两个课题组的凭据运行一遍流程。

1. 在 CPU 节点登记合成的 VASP 优化结构、能量与后处理脚本。这里的作业、结构、能量及收敛证据都是虚构数据。
2. 合作组按 `CCuO / PBE / TOTEN / 比较指纹` 检索，读取结果来源和文件 SHA-256。脚本有两个版本，合作组只获授权取得 v1。
3. 在独立分析目录按固定版本下载 `CONTCAR`、`energy.json` 和脚本，校验后执行内置合成脚本。报告保留结构结果与脚本的版本引用。
4. 重启 HTTP 服务并重新打开目录，验证结果可读。撤销合作组对原始结构结果的授权，验证检索、详情和下载均收回，包括依赖它的派生报告。

## 用 MCP 客户端试用

把 [demo.py](demo.py) 顶部的 `serve_catalog` 设为 `True` 后运行同一命令。服务只绑定 `127.0.0.1`，启动时在终端打印地址和两个本次进程有效的演示凭据，退出即结束。客户端使用 Streamable HTTP，地址为 `http://127.0.0.1:8879/mcp`，按终端提示配置 `Authorization: Bearer <凭据>`。可给两个 MCP 客户端分别配置两个课题组凭据。

| MCP 工具 | 参数示例 |
| --- | --- |
| `asset_search` | `{"kind":"calculation","formula":"CCuO","functional":"PBE","energy_metric":"TOTEN"}` |
| `asset_inspect` | `{"revision":"检索结果中的完整 revision"}` |
| `asset_fetch` | `{"revision":"检索结果中的完整 revision","name":"CONTCAR"}` |

MCP 只提供读操作。登记、人工复核和共享演示由 Python 场景驱动。单文件上限为 64 KiB，SQLite 内的小文件按内容去重。命令模式使用真实本机 HTTP 与两个模拟身份，执行的是内置、经字节比对的合成脚本。接入真实节点和账号时，需把身份、数据获取和托管存储接到 Chem-Agent。 [科研资产使用说明](../../docs/RESEARCH_ASSETS.md)
