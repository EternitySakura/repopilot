"""Human and machine reports from controller state, never from model completion claims."""

from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

from repopilot.agent.loop import LoopOutcome, LoopState
from repopilot.contracts.run import (
    LLMAccounting,
    RunAccounting,
    RunCounters,
    RunLimits,
    RunReport,
    RunStatus,
)
from repopilot.observability.events import EventLog
from repopilot.settings import Settings


class RunTracker:
    def __init__(self, events: EventLog, settings: Settings, *, mode: str, task_id: str | None = None,
                 accounting: RunAccounting | LLMAccounting | None = None, source_run_id: str | None = None,
                 state: LoopState | None = None, started_at: str | None = None,
                 elapsed_seconds: float = 0, resuming: bool = False):
        self.events = events
        self.settings = settings
        self.mode = mode
        self.task_id = task_id
        self.state = state if state is not None else LoopState()
        self.started_at = started_at if started_at is not None else datetime.now(UTC).isoformat()
        self.started = perf_counter() - elapsed_seconds
        self.accounting = accounting if accounting is not None else RunAccounting()
        self.limits = RunLimits.model_validate({name: getattr(settings, name)
                                               for name in RunLimits.model_fields})
        model_mode = ("imported" if mode == "imported_candidate" else "reconstructed" if mode == "reconstructed_candidate"
                      else "controller" if mode == "holdout_reference" else "live" if accounting is not None else "scripted")
        linkage = {"source_run_id": source_run_id} if source_run_id is not None else {}
        if not resuming:
            events.write("run.started", mode=mode, model_mode=model_mode, **linkage,
                         max_iterations=settings.max_iterations, limits=self.limits.model_dump(),
                         accounting=self.accounting.model_dump())

    def finish(self, outcome: LoopOutcome | None, *, generation: int = 0,
               patch_ref: str | None = None, diagnostic: str | None = None,
               failure_reason: str = "program_error") -> RunReport:
        status = RunStatus.FAILED if diagnostic is not None else outcome.status
        reason = failure_reason if diagnostic is not None else outcome.stop_reason
        counts = RunCounters.model_validate({name: getattr(self.state, name) for name in RunCounters.model_fields})
        test = self.state.last_test
        assessment = "not_tested"
        if test is not None and test.ok and diagnostic is None:
            if test.workspace_generation != generation or test.data.get("stale"):
                assessment = "stale_test_results"
            elif test.data.get("exit_code") == 0 and test.data.get("public_tests_passed") is True:
                assessment = "public_tests_passed"
            elif test.data.get("exit_code") == 1:
                assessment = "public_tests_failed"
        artifacts = {"events": str(self.events.path)}
        for name in ("tools", "artifacts", "executions", "derivation", "import"):
            path = self.events.directory / f"{name}.json"
            if path.is_file():
                artifacts[name] = str(path)
        profile_path = self.events.directory / "profiles.json"
        if profile_path.is_file():
            artifacts["profiles"] = str(profile_path)
        if patch_ref is not None:
            artifacts["patch"] = patch_ref
            manifest_path = Path(patch_ref).with_suffix(".manifest.json")
            if manifest_path.is_file():
                artifacts["candidate_manifest"] = str(manifest_path)
        if test is not None and test.artifact_ref:
            artifacts["test_log"] = test.artifact_ref
        report = RunReport(
            run_id=self.events.run_id, mode=self.mode, task_id=self.task_id,
            status=status, stop_reason=reason, started_at=self.started_at,
            finished_at=datetime.now(UTC).isoformat(),
            duration_ms=int((perf_counter() - self.started) * 1000), counters=counts, limits=self.limits,
            accounting=self.accounting, workspace_generation=generation,
            last_test_execution_id=test.data.get("execution_id") if test is not None and diagnostic is None else None,
            tested_input_manifest_sha256=test.data.get("input_manifest_sha256") if test is not None and diagnostic is None else None,
            candidate_assessment=assessment, public_tests_passed=assessment == "public_tests_passed",
            failure_fingerprints=dict(self.state.failure_fingerprints), artifacts=artifacts,
            diagnostic=diagnostic,
            model_failure=self.events.model_failure,
            evaluation_context=self.events.evaluation_context,
        )
        directory = self.events.directory
        (directory / "run.json").write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
        (directory / "report.md").write_text(render_report(report), encoding="utf-8")
        self.events.write(
            "run.finished", status=report.status.value, stop_reason=report.stop_reason,
            bug_fixed=False, independently_verified=False, public_tests_passed=report.public_tests_passed,
            workspace_generation=generation, accounting=self.accounting.model_dump(),
            result={"tool_calls": counts.tool_calls, "decisions": counts.decisions},
            counters=counts.model_dump(), candidate_assessment=report.candidate_assessment,
            patch_ref=patch_ref, diagnostic=diagnostic,
            report_ref=str(directory / "report.md"), summary_ref=str(directory / "run.json"),
            report=report.model_dump(mode="json"),
        )
        return report


def render_report(report: RunReport) -> str:
    accounting_text = "模型为脚本模拟；Token 用量和费用不适用（JSON 中为 null，未记为实测零费用）。"
    if report.mode == "reconstructed_candidate":
        accounting_text = "本次仅从记录重建新候选，没有模型调用；源运行用量与未完成记录保持在源运行中，未重置或转为零费用。"
    if report.mode == "imported_candidate":
        accounting_text = "本次从公开交付包导入新候选，没有模型调用；包内历史验证仅作来源记录，本候选尚未独立验证。"
    if report.mode == "holdout_reference":
        accounting_text = "本次为控制端参考修复的留出任务验收，模型调用为零。"
    if isinstance(report.accounting, LLMAccounting):
        measured = report.accounting
        accounting_text = (
            f"DeepSeek 模型：{measured.requested_model}；请求数：{measured.requests}；"
            f"usage 状态：{measured.usage_status}；已知输入/输出 Token："
            f"{measured.known_prompt_tokens}/{measured.known_completion_tokens}。"
            f"按费率快照计算的费用上界：USD {measured.cost_upper_bound_usd}，"
            f"配置上限：USD {measured.max_cost_usd}。费用上界不是实际账单；usage 缺失不记零。"
        )
    assessments = {
        "not_tested": "当前候选尚未获得有效公开测试结论。",
        "public_tests_passed": "当前候选公开测试通过，尚未独立验证。",
        "public_tests_failed": "当前候选公开测试未通过。",
        "stale_test_results": "测试后代码发生变化，先前测试结果已失效，当前候选未验证。",
    }
    lines = [
        "# RepoPilot 运行报告", "", f"运行 ID：`{report.run_id}`",
        f"任务：`{report.task_id or '只读演示'}`", f"停止状态：`{report.status.value}`",
        f"停止原因：`{report.stop_reason}`", "", assessments[report.candidate_assessment],
        "最终 Gate B 尚未实现；脚本结束或模型声明不能证明 Bug 已修复。", "",
        f"开始时间（UTC）：{report.started_at}", f"结束时间（UTC）：{report.finished_at}",
        f"运行耗时：{report.duration_ms} ms", f"工作区代数：{report.workspace_generation}", "",
        (f"循环总时限：{report.limits.max_run_seconds if report.limits.max_run_seconds is not None else '未设置'} 秒；"
         "在决策和工具调用边界检查，阻塞等待受自身超时及剩余时限约束。"), "",
        f"最后测试执行 ID：`{report.last_test_execution_id or '无有效记录'}`",
        f"测试输入清单 SHA-256：`{report.tested_input_manifest_sha256 or '无有效记录'}`", "",
        "| 计数 | 已用 | 上限 |", "| --- | ---: | ---: |",
        f"| 模型决策 | {report.counters.decisions} | {report.limits.max_iterations} |",
        f"| 工具调用 | {report.counters.tool_calls} | {report.limits.max_tool_calls} |",
        f"| 编辑尝试 | {report.counters.edit_calls} | {report.limits.max_edit_calls} |",
        f"| 测试尝试 | {report.counters.test_calls} | {report.limits.max_test_calls} |",
        f"| 连续无进展 | {report.counters.no_progress_streak} | {report.limits.max_no_progress} |",
        "", f"工具调用失败次数：{report.counters.failed_tool_calls}",
        accounting_text, "",
        "## 制品", "",
    ]
    for name, path in report.artifacts.items():
        href = path.replace("\\", "/")
        lines.append(f"- {name}：[{name}](<{href}>)")
    if report.model_failure is not None:
        failure = report.model_failure
        metadata = failure.response_diagnostic or {}
        lines.extend(["", "## 模型响应诊断", "", f"调用 ID：`{failure.tool_call_id}`",
                      f"错误码：`{failure.code}`", f"调用耗时：{failure.duration_ms if failure.duration_ms is not None else '未知'} ms",
                      "原始响应正文未保存；仅记录控制端提取的格式特征和计量。"])
        if metadata:
            lines.extend([f"结束标识：`{metadata.get('finish_reason', '未知')}`",
                          f"响应类型：`{metadata.get('content_type', '未知')}`；字节数：{metadata.get('content_bytes', '未知')}",
                          f"内容 SHA-256：`{metadata.get('content_sha256', '未记录')}`",
                          f"JSON 错误类别：`{metadata.get('json_error_kind', '未进入语法解析')}`"])
            if "json_error_line" in metadata:
                lines.append(f"JSON 解析位置：第 {metadata['json_error_line']} 行，第 {metadata['json_error_column']} 列；偏移 {metadata['json_error_position']}")
            if "json_trailing_kind" in metadata:
                lines.append(f"附加内容类别：`{metadata['json_trailing_kind']}`；字符数：{metadata['json_trailing_chars']}。正文未保存。")
            lines.append("JSON 代码围栏 / BOM / 空内容："
                         f"{metadata.get('starts_fence', '未知')} / {metadata.get('starts_bom', '未知')} / {metadata.get('empty', '未知')}")
    if report.failure_fingerprints:
        lines.extend(["", "## 失败指纹", "", "| SHA-256 | 次数 |", "| --- | ---: |"])
        lines.extend(f"| `{key}` | {count} |" for key, count in sorted(report.failure_fingerprints.items()))
    if report.diagnostic:
        lines.extend(["", "## 异常诊断", "", "```text", report.diagnostic.rstrip(), "```"])
    return "\n".join(lines) + "\n"
