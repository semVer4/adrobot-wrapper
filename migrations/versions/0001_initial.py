"""initial"""

from alembic import op
import sqlalchemy as sa


revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('streams',
        sa.Column('id', sa.Integer(), autoincrement=False, nullable=False),
        sa.Column('campaign_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('missing', sa.Boolean(), nullable=False),
        sa.Column('conflict', sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_streams_campaign_id'), 'streams', ['campaign_id'], unique=False)
    op.create_table('stream_offers',
        sa.Column('stream_id', sa.Integer(), nullable=False),
        sa.Column('offer_id', sa.Integer(), autoincrement=False, nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('share', sa.Integer(), nullable=False),
        sa.Column('pinned', sa.Boolean(), nullable=False),
        sa.Column('archived', sa.Boolean(), nullable=False),
        sa.Column('published', sa.Boolean(), nullable=False),
        sa.Column('published_share', sa.Integer(), nullable=False),
        sa.Column('published_pinned', sa.Boolean(), nullable=False),
        sa.Column('published_archived', sa.Boolean(), nullable=False),
        sa.CheckConstraint('published_share BETWEEN 0 AND 100', name='ck_stream_offers_published_share'),
        sa.CheckConstraint('share BETWEEN 0 AND 100', name='ck_stream_offers_share'),
        sa.ForeignKeyConstraint(['stream_id'], ['streams.id'], ),
        sa.PrimaryKeyConstraint('stream_id', 'offer_id')
    )


def downgrade():
    op.drop_table('stream_offers')
    op.drop_index(op.f('ix_streams_campaign_id'), table_name='streams')
    op.drop_table('streams')
