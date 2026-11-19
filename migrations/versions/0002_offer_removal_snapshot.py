"""Keep the draft allocation while an offer removal is pending."""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("stream_offers", sa.Column("removal_snapshot", sa.JSON(), nullable=True))


def downgrade():
    op.drop_column("stream_offers", "removal_snapshot")
