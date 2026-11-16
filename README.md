# RepoPilot

RepoPilot 是一个 Python 仓库修复工具。它在独立工作区生成候选补丁，再从固定基线重建候选、运行原始公开测试，记录独立 Gate A 判定。当前稳定版为 **v0.2.0**，提供 12 个公开开发任务。

[下载稳定版](https://github.com/EternitySakura/repopilot/releases/latest) · [浏览 v0.2.0 源码](https://github.com/EternitySakura/repopilot/tree/v0.2.0) · [查看变更记录](https://github.com/EternitySakura/repopilot/blob/v0.2.0/CHANGELOG.md)

## 快速开始

准备 Python 3.11、uv 和 Git。下载并解包发行页的 `repopilot-v0.2.0-public-source.zip`，在**解包后的源码目录**运行：

```powershell
uv sync --locked
uv run --no-sync repopilot --list-tasks --runtime-version 2
uv run --no-sync repopilot --fake-demo
```

演示执行真实的列目录、搜索和文件读取，无需 Docker 或模型密钥。

## 独立验证示例

按 v2 环境指南构建 `linux/amd64` 镜像。将同一发行页的 `repopilot-demo-v2-candidate.zip` 放入源码目录，然后运行：

```powershell
uv run --no-sync python scripts/import_candidate.py --bundle repopilot-demo-v2-candidate.zip --verify
```

命令从固定基线重建候选，在受限 Docker 中运行原始公开测试，并返回新验证编号。遇到 `DOCKER_UNAVAILABLE` 时，先确认 Docker 的 Linux 引擎正在运行，已启用 containerd 镜像库，然后按指南核对镜像 ID。[v2 镜像构建与使用](https://github.com/EternitySakura/repopilot/blob/v0.2.0/docs/runtime-v2.md)

## 迁移 v1 候选

从 v0.1.0 的公开评测资料 ZIP 提取 `repopilot-demo-candidate.zip`，在 v0.2.0 源码目录和 v2 镜像就绪后运行：

```powershell
uv run --no-sync python scripts/import_candidate.py --bundle repopilot-demo-candidate.zip --runtime-version 2 --verify
```

需要先离线迁移时，省略 `--verify`，随后用 `repopilot --db-verify RUN_ID` 执行独立验证。[v0.1.0 公开评测资料](https://github.com/EternitySakura/repopilot/releases/tag/v0.1.0)

源码采用 [MIT 许可证](https://github.com/EternitySakura/repopilot/blob/v0.2.0/LICENSE)。
