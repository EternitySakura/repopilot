"""Frozen revision 0001 schema. Future changes belong in a new migration."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

metadata = sa.MetaData(naming_convention={"pk": "pk_%(table_name)s", "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
                                        "uq": "uq_%(table_name)s_%(column_0_name)s"})


def json_type():
    return sa.JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql")


tasks = sa.Table("rp_tasks", metadata,
    sa.Column("spec_sha256", sa.String(64), primary_key=True),
    sa.Column("task_key", sa.Text, nullable=False),
    sa.Column("repo_profile", sa.Text, nullable=False),
    sa.Column("base_revision", sa.String(64), nullable=False),
    sa.Column("spec", json_type(), nullable=False),
    sa.Column("created_at", sa.Text, nullable=False))

runs = sa.Table("rp_runs", metadata,
    sa.Column("run_id", sa.String(32), primary_key=True),
    sa.Column("task_sha256", sa.String(64), sa.ForeignKey("rp_tasks.spec_sha256")),
    sa.Column("mode", sa.Text),
    sa.Column("status", sa.Text, nullable=False, server_default="running"),
    sa.Column("source_run_id", sa.String(32)),
    sa.Column("last_seq", sa.Integer, nullable=False, server_default="0"),
    sa.Column("workspace_generation", sa.Integer, nullable=False, server_default="0"),
    sa.Column("report", json_type()),
    sa.Column("artifact_directory", sa.Text, nullable=False),
    sa.Column("created_at", sa.Text, nullable=False),
    sa.Column("finished_at", sa.Text),
    sa.CheckConstraint("last_seq >= 0 AND workspace_generation >= 0", name="ck_rp_runs_counts"))

events = sa.Table("rp_events", metadata,
    sa.Column("run_id", sa.String(32), sa.ForeignKey("rp_runs.run_id"), primary_key=True),
    sa.Column("seq_no", sa.Integer, primary_key=True),
    sa.Column("event_type", sa.Text, nullable=False),
    sa.Column("occurred_at", sa.Text, nullable=False),
    sa.Column("payload_sha256", sa.String(64), nullable=False),
    sa.Column("payload", json_type(), nullable=False),
    sa.CheckConstraint("seq_no > 0", name="ck_rp_events_seq"))

calls = sa.Table("rp_calls", metadata,
    sa.Column("run_id", sa.String(32), sa.ForeignKey("rp_runs.run_id"), primary_key=True),
    sa.Column("call_id", sa.String(64), primary_key=True),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("intent_sha256", sa.String(64), nullable=False),
    sa.Column("arguments", json_type()),
    sa.Column("result", json_type()),
    sa.Column("error_code", sa.Text),
    sa.Column("reserved_cost_usd", sa.Numeric(24, 12)),
    sa.Column("cost_upper_bound_usd", sa.Numeric(24, 12)),
    sa.Column("usage_status", sa.Text),
    sa.Column("prompt_tokens", sa.BigInteger),
    sa.Column("completion_tokens", sa.BigInteger),
    sa.Column("created_at", sa.Text, nullable=False),
    sa.Column("completed_at", sa.Text),
    sa.CheckConstraint("kind IN ('tool', 'model', 'gate_a')", name="ck_rp_calls_kind"),
    sa.CheckConstraint("status IN ('intent', 'completed', 'failed', 'unknown')", name="ck_rp_calls_status"),
    sa.CheckConstraint("reserved_cost_usd IS NULL OR reserved_cost_usd >= 0", name="ck_rp_calls_reserved"),
    sa.CheckConstraint("cost_upper_bound_usd IS NULL OR cost_upper_bound_usd >= 0", name="ck_rp_calls_cost"),
    sa.CheckConstraint("prompt_tokens IS NULL OR prompt_tokens >= 0", name="ck_rp_calls_input"),
    sa.CheckConstraint("completion_tokens IS NULL OR completion_tokens >= 0", name="ck_rp_calls_output"))

executions = sa.Table("rp_executions", metadata,
    sa.Column("run_id", sa.String(32), primary_key=True),
    sa.Column("execution_id", sa.String(32), primary_key=True),
    sa.Column("call_id", sa.String(64), nullable=False),
    sa.Column("scope", sa.Text, nullable=False),
    sa.Column("state", sa.Text, nullable=False),
    sa.Column("cleanup_status", sa.Text, nullable=False),
    sa.Column("source_generation", sa.Integer, nullable=False),
    sa.Column("input_manifest_sha256", sa.String(64), nullable=False),
    sa.Column("record", json_type(), nullable=False),
    sa.ForeignKeyConstraint(["run_id", "call_id"], ["rp_calls.run_id", "rp_calls.call_id"]),
    sa.UniqueConstraint("run_id", "execution_id", "call_id", "input_manifest_sha256", name="uq_rp_execution_input"),
    sa.UniqueConstraint("run_id", "call_id", name="uq_rp_execution_call"),
    sa.CheckConstraint("source_generation >= 0", name="ck_rp_execution_generation"),
    sa.CheckConstraint("state IN ('preparing','running','completed','failed','timed_out','unknown')", name="ck_rp_execution_state"),
    sa.CheckConstraint("cleanup_status IN ('not_needed','pending','confirmed','unknown')", name="ck_rp_execution_cleanup"),
    sa.CheckConstraint("state <> 'completed' OR cleanup_status = 'confirmed'", name="ck_rp_execution_completed"))

test_results = sa.Table("rp_test_results", metadata,
    sa.Column("run_id", sa.String(32), primary_key=True),
    sa.Column("execution_id", sa.String(32), primary_key=True),
    sa.Column("call_id", sa.String(64), nullable=False),
    sa.Column("input_manifest_sha256", sa.String(64), nullable=False),
    sa.Column("state", sa.Text, nullable=False),
    sa.Column("exit_code", sa.Integer),
    sa.Column("passed", sa.Integer),
    sa.Column("failed", sa.Integer),
    sa.Column("public_tests_passed", sa.Boolean, nullable=False),
    sa.Column("summary", json_type(), nullable=False),
    sa.ForeignKeyConstraint(["run_id", "execution_id", "call_id", "input_manifest_sha256"],
        ["rp_executions.run_id", "rp_executions.execution_id", "rp_executions.call_id", "rp_executions.input_manifest_sha256"]),
    sa.CheckConstraint("passed IS NULL OR passed >= 0", name="ck_rp_tests_passed"),
    sa.CheckConstraint("failed IS NULL OR failed >= 0", name="ck_rp_tests_failed"),
    sa.CheckConstraint("NOT public_tests_passed OR (state = 'completed' AND exit_code = 0)", name="ck_rp_tests_verdict"))

artifacts = sa.Table("rp_artifacts", metadata,
    sa.Column("run_id", sa.String(32), sa.ForeignKey("rp_runs.run_id"), primary_key=True),
    sa.Column("artifact_id", sa.String(32), primary_key=True),
    sa.Column("execution_id", sa.String(32)),
    sa.Column("producer_call_id", sa.String(64)),
    sa.Column("input_manifest_sha256", sa.String(64)),
    sa.Column("relative_path", sa.Text, nullable=False),
    sa.Column("visibility", sa.Text, nullable=False),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("sha256", sa.String(64), nullable=False),
    sa.Column("size_bytes", sa.BigInteger, nullable=False),
    sa.Column("record", json_type(), nullable=False),
    sa.UniqueConstraint("run_id", "relative_path", name="uq_rp_artifact_path"),
    sa.ForeignKeyConstraint(["run_id", "execution_id", "producer_call_id", "input_manifest_sha256"],
        ["rp_executions.run_id", "rp_executions.execution_id", "rp_executions.call_id", "rp_executions.input_manifest_sha256"]),
    sa.CheckConstraint("size_bytes >= 0", name="ck_rp_artifact_size"),
    sa.CheckConstraint("visibility IN ('agent','controller','report')", name="ck_rp_artifact_visibility"),
    sa.CheckConstraint("kind <> 'public_test_log' OR (execution_id IS NOT NULL AND producer_call_id IS NOT NULL AND input_manifest_sha256 IS NOT NULL)",
                       name="ck_rp_artifact_provenance"))
