"""drop formality, style tags and generation occasion

Revision ID: c3e8f1a2b4d6
Revises: b7c4d2e91a10
Create Date: 2026-10-07 13:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'c3e8f1a2b4d6'
down_revision: str | None = 'b7c4d2e91a10'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint('ck_items_formality', 'items', type_='check')
    op.drop_column('items', 'formality')
    op.drop_column('user_preferences', 'formality_bias')
    op.drop_column('user_preferences', 'style_tags')
    op.drop_column('outfits', 'occasion')


def downgrade() -> None:
    op.add_column('outfits', sa.Column('occasion', sa.String(), nullable=True))
    op.add_column(
        'user_preferences',
        sa.Column('style_tags', postgresql.ARRAY(sa.Text()), server_default='{}', nullable=False),
    )
    op.add_column(
        'user_preferences',
        sa.Column('formality_bias', sa.Float(), server_default='0', nullable=False),
    )
    op.add_column('items', sa.Column('formality', sa.Integer(), nullable=True))
    op.create_check_constraint('ck_items_formality', 'items', 'formality BETWEEN 1 AND 5')
