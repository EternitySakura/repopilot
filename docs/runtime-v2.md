# 运行环境 v2

12 个公开开发任务均可选用运行环境 v2。构建使用 Python 3.11.18 与固定 pytest 依赖。构建上下文包含 Dockerfile 和带 wheel SHA-256 的依赖锁。基础镜像通过 Docker Hub 摘要固定。构建会从 PyPI 获取并校验所需 wheel。

使用 Docker Linux 引擎、Buildx 和 containerd 镜像库。以下命令以 Docker 29.9.3、Buildx 0.38.1、`linux/amd64` 验收。从 RepoPilot 根目录执行：

```powershell
docker buildx build --platform linux/amd64 --provenance=false --sbom=false --build-arg SOURCE_DATE_EPOCH=0 --output 'type=image,name=repopilot-test-runtime:v2,store=true,oci-mediatypes=true,rewrite-timestamp=true,compatibility-version=20' scripts/release/runtime-v2
docker image inspect repopilot-test-runtime:v2 --format '{{.Id}} {{.Os}}/{{.Architecture}}'
```

输出应为 `sha256:26581a598d8630859a18a3ee6ee399aaee26f921e84ccd48c765ed975797a04e linux/amd64`。RepoPilot 按该镜像 ID 选择运行环境。

列出 v2 任务，并查询其中一个任务的公开源码定义：

```powershell
uv run --no-sync repopilot --list-tasks --runtime-version 2
uv run --no-sync repopilot --find-symbol clamp --task-id rp-runtime-v2-002
```

用现有演示命令生成候选，再用输出的运行编号发起独立 Gate A：

```powershell
uv run --no-sync repopilot --repair-demo --task-id rp-runtime-v2-001
uv run --no-sync repopilot --batch-demo --task-id rp-runtime-v2-006
uv run --no-sync repopilot --verify-run RUN_ID
```

如需使用已配置的模型执行 v2 任务，在 `--live-run` 时传入列表中的任务 ID，例如 `--task-id rp-runtime-v2-002`。真实运行会发送任务摘要与公开工具反馈，并产生 API 用量。

v2 使用 `rp-runtime-v2-001` 到 `rp-runtime-v2-012` 的任务 ID，并绑定各自的 `@2` 仓库配置。独立 Gate A 沿用原始公开测试，验证报告记录镜像摘要。默认任务列表、v0.1.0 发行候选和历史评测按原 v1 任务登记解析。

## 迁移 v1 交付包

构建 v2 镜像后，可以从 v1 公开交付包生成新候选并重新验证：

```powershell
uv run --no-sync python scripts/import_candidate.py --bundle repopilot-demo-candidate.zip --runtime-version 2 --verify
```

控制端先按 v1 登记核对原包，再以对应的 v2 任务绑定同一份补丁。新候选拥有单独的运行编号，`--verify` 执行一次独立 Gate A 并给出新验证编号。省略 `--verify` 时可先复核导入记录，再用 `--db-verify RUN_ID` 执行测试。可在 `--db-inspect RUN_ID` 的 `import.provenance.runtime_migration` 中查看来源与目标。

[Python 官方镜像](https://hub.docker.com/_/python) · [BuildKit 可复现构建选项](https://github.com/moby/buildkit/blob/master/docs/build-repro.md) · [pip 哈希校验](https://pip.pypa.io/en/stable/topics/secure-installs/)
