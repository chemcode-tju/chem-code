# 本地计算平台

Chem Code 使用平台管理的方案、文件和 Slurm 作业，不再包含 AiiDA worker、daemon、ORM 或 WorkChain 执行插件。VASP、ORCA、LAMMPS 等通过 task/job 节点及普通提交脚本运行；VASP 计算功能没有被移除。

实现与工具说明见 [通用计算平台](COMPUTATION_PLATFORM.md)，结果关联见 [计算结果登记](RESULT_REGISTRY.md)，结构与轨迹见 [查看器](structure-viewer.md)。

## 启用与配置

启用 `CHEMISTRY_AUTOMATION_ENABLED=true` 后，在本地设置页选择已有 OpenSSH 主机。服务器发现、软件路径、环境脚本、赝势和作业输入按任务在平台登记；不需要初始化额外计算引擎或安装 VASP 工作流插件。共享账号服务仍不开放本地 SSH 执行能力。

可选运行依赖在 `automation` extra 中，包括 SSH 连接支持和绘图依赖；不存在 `aiida` extra。项目安装方法见 README。

## 数据路径与历史记录

- 新设置名为 `CHEMISTRY_PLATFORM_PATH`，代码使用 `Settings.platform_path`。
- 为保持现有数据可见，默认目录仍为项目的 `data/aiida`。这只是历史目录名称，不再表示使用该引擎；本次清理不会搬动或删除目录中的文件。
- 已配置的 `CHEMISTRY_AIIDA_PATH` 仅作为数据路径别名继续读取；同时设置新旧变量时，新变量优先。
- 结构、模型、方案、作业收据、分析产物、SSH 配置和事件记录保持原位置。旧引擎历史方案可读但不能恢复执行，也不会重提交到 Slurm。
- 历史结果已取回的文件仍可访问；依赖已删除 ORM 的远端实时查询不可用，不能把不可用误报成已完成。
- 新研究从当前 task/job 方案开始；不要通过重命名旧节点或伪造运行状态来迁移科研记录。

## 验证与备份

备份应同时覆盖聊天数据库、配置中的平台数据目录及工作区文件。Git 只保存代码，不替代研究数据备份。

测试使用临时目录和模拟服务，不提交真实作业：

```sh
PYTHONPATH=src python -m pytest -q
node --test tests/*.test.mjs tests/structure/*.cjs
```

本地 SSH/登录回调测试需要允许 loopback 和 Unix socket 的测试环境。作业调度完成、结果解析完成和科学收敛仍是独立状态。
