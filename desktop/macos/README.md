# Chem Code macOS 版

Apple Silicon（M 系列）和 macOS 14.0 或更新版本。打开 DMG，将 **Chem Code.app** 拖入「应用程序」，再双击打开。应用包含 Python、RDKit、ASE、SciPy、SSH 依赖与工作台资源。

- 打开应用即可使用本机工作台，无需注册或登录 Chem Code。首次启动会检测已安装并授权的 Codex 和 Antigravity。尚未授权时，可先用文件与结构工具，再在「设置 → Agent 与模型」配置已有 CLI 或模型服务。程序路径也可手动调整。
- 窗口采用 macOS 一体化隐藏标题栏，红黄绿窗口控制融入顶部工具区并同步明暗外观、窗口尺寸与位置。支持 `⌘N` 新建对话、`⌘\` 切换侧边栏、`⌘,` 打开设置与 `⌘+`/`⌘-`/`⌘0` 缩放，顶部空白处可拖动和双击缩放窗口。
- 「设置 → 服务器与环境」会发现 `~/.ssh/config` 中的主机别名。启用所需节点后再连接。对话的运行位置栏可选择本机或远端研究文件夹。VASP、CP2K、LAMMPS 等计算软件由目标计算环境提供。
- 会话、项目、模型配置和日志保存在当前 macOS 用户的独立工作区，默认位于 `~/Library/Application Support/Chem Code`。菜单「Chem Code → 更改工作区位置」可选择其他私有目录，下次打开生效。切换时旧数据保留在原处。
- 系统账户隔离工作区文件，应用为每个工作区保留独立的 WebKit 缓存和草稿。桌面服务只监听回环地址，并由应用自动建立访问会话。Codex 复用当前用户的原生授权，Antigravity 使用系统凭据存储。应用为各自的会话保存独立运行状态。
- 关闭窗口后，应用仍可处理定时监控。用 `⌘Q` 退出后，本地后台服务随应用关闭。需要无人值守时可继续使用常驻服务部署。
- 本地知识 MCP 可以使用「设置 → 知识与技能 → MCP 配置」中的 stdio JSON。Streamable HTTP 客户端连接桌面地址时，需要用数据目录中的 `knowledge-mcp.token` 配置 Bearer 认证。该令牌只适用于知识 MCP。

## 构建

在 Apple Silicon Mac 上使用 Xcode Command Line Tools、`uv` 和有 Pillow 的 Python 3.11 环境运行：

```sh
python scripts/package_macos.py
```

DMG 和 SHA-256 校验文件生成到 `output/macos/`。构建会检查内置 Mach-O 的架构与最低系统版本，并校验应用和磁盘映像。当前使用本地临时签名。对外发行需要 Developer ID 签名和 Apple 公证。[Apple 分发文档](https://developer.apple.com/documentation/xcode/packaging-mac-software-for-distribution)
