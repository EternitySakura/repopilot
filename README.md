# RepoPilot v0.1 · 公开源码版

此发行包提供 RepoPilot CLI、12 个开发任务的公开 Fixture、补丁和公开 Gate A 验证。Gate A 从原始基线重建候选并执行固定公开测试。留出评测、私有 Gate B 和参考解使用单独的控制端发行物。

## 快速开始

在解包目录准备 Python 3.11、uv 0.12.18 和 Git，然后运行：

```powershell
uv sync --locked
uv run --no-sync repopilot --list-tasks
uv run --no-sync repopilot --fake-demo
```

只读演示实际执行目录查询、字面搜索和文件读取。使用 Python 查看候选 ZIP 或 Git 读取补丁均无需 Docker。

## 候选与公开验证

将公开验收资料中的 `repopilot-demo-candidate.zip` 放在发行包根目录。核对并展开候选的命令无需模型密钥：

```powershell
uv run --no-sync python scripts/export_candidate.py --check repopilot-demo-candidate.zip
uv run --no-sync python scripts/export_candidate.py --materialize repopilot-demo-candidate.zip --output artifacts/test-work/review
```

重新验证时需启动 Docker Linux 引擎。Docker Desktop 须启用 containerd 镜像库，并从单独交付的容器归档加载原冻结镜像：

```powershell
docker image load --input repopilot-fixture-docker-save.tar
docker image inspect sha256:a5901493e7d36bd81679d2cd7c5e0d1117a7adedf8165194c9e165ca5003224f
uv run --no-sync python scripts/import_candidate.py --bundle repopilot-demo-candidate.zip --verify
```

`--verify` 用本机 SQLite 账本保留新的 Gate A 判定。也可运行 `uv run --no-sync python scripts/demo_week1.py`，查看简单修复、错误候选对照和预算停止。

公开版与正式评测控制端有不同的源码指纹。正式留出集的 108 槽结果见单独交付的公开验收资料。发行文件清单和 SHA-256 见 `PUBLIC-SOURCE-MANIFEST.json`。

[MIT 许可证](LICENSE)
