# RepoPilot v0.2.0 · 公开源码版

此发行包提供 RepoPilot CLI、12 个开发任务的公开 Fixture、补丁和公开 Gate A 验证。Gate A 从原始基线重建候选并执行固定公开测试。留出评测、私有 Gate B 和参考解使用单独的控制端发行物。

## 快速开始

在解包目录准备 Python 3.11、uv 0.12.18 和 Git，然后运行：

```powershell
uv sync --locked
uv run --no-sync repopilot --list-tasks --runtime-version 2
uv run --no-sync repopilot --fake-demo
```

只读演示实际执行目录查询、字面搜索和文件读取。使用 Python 查看候选 ZIP 或 Git 读取补丁均无需 Docker。

## 候选与公开验证

先按[运行环境 v2](docs/runtime-v2.md)构建固定测试镜像。将发行附件中的 `repopilot-demo-v2-candidate.zip` 放在源码目录。核对、展开和重新验证候选均无需模型密钥：

```powershell
uv run --no-sync python scripts/export_candidate.py --check repopilot-demo-v2-candidate.zip
uv run --no-sync python scripts/export_candidate.py --materialize repopilot-demo-v2-candidate.zip --output artifacts/test-work/review
uv run --no-sync python scripts/import_candidate.py --bundle repopilot-demo-v2-candidate.zip --verify
```

`--verify` 用本机 SQLite 账本保留新的 Gate A 判定。

### 迁移 v0.1.0 示例候选

从 [v0.1.0 发行页](https://github.com/EternitySakura/repopilot/releases/tag/v0.1.0) 下载公开评测证据 ZIP，提取其中的 `repopilot-demo-candidate.zip`。构建 v2 镜像后，运行：

```powershell
uv run --no-sync python scripts/import_candidate.py --bundle repopilot-demo-candidate.zip --runtime-version 2 --verify
```

命令用同一份补丁创建 v2 候选，并执行一次独立 Gate A。省略 `--verify` 可离线核对并迁移，在以后通过 `--db-verify RUN_ID` 执行公开测试。

公开版与正式评测控制端有不同的源码指纹。正式留出集的 108 槽结果见单独交付的公开验收资料。发行文件清单和 SHA-256 见 `PUBLIC-SOURCE-MANIFEST.json`。

[MIT 许可证](LICENSE)
