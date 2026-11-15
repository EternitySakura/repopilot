"""Alembic entry point: connection credentials stay in the controlling process."""

from alembic import context

from repopilot.ledger.schema_v1 import metadata

connection = context.config.attributes.get("connection")
if connection is None:
    raise RuntimeError("Use repopilot --db-init to supply a controller-owned connection")
context.configure(connection=connection, target_metadata=metadata, version_table="rp_alembic_version",
                  compare_type=True)
with context.begin_transaction():
    context.run_migrations()
