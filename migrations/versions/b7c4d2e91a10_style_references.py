"""style references

Revision ID: b7c4d2e91a10
Revises: 341fad50825e
Create Date: 2026-10-07 12:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'b7c4d2e91a10'
down_revision: str | None = '341fad50825e'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'style_references',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('image_path', sa.String(), nullable=False),
        sa.Column('style', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_style_references_user_id'), 'style_references', ['user_id'], unique=False)
    op.add_column(
        'user_preferences',
        sa.Column('use_style_references', sa.Boolean(), server_default='false', nullable=False),
    )


def downgrade() -> None:
    op.drop_column('user_preferences', 'use_style_references')
    op.drop_index(op.f('ix_style_references_user_id'), table_name='style_references')
    op.drop_table('style_references')
