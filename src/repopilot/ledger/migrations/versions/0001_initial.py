"""Initial controller run ledger; no checkpoint or recovery tables."""

from alembic import op

from repopilot.ledger.schema_v1 import metadata

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    metadata.create_all(op.get_bind(), checkfirst=False)


def downgrade():
    metadata.drop_all(op.get_bind(), checkfirst=False)
