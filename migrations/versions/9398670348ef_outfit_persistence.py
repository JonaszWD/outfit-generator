"""outfit persistence

Revision ID: 9398670348ef
Revises: a1f2c3d4e5f6
Create Date: 2026-10-06 16:16:17.488913
"""
from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '9398670348ef'
down_revision: str | None = 'a1f2c3d4e5f6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('outfits', sa.Column('context', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column('outfits', sa.Column('breakdown', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column('outfits', sa.Column('favorite', sa.Boolean(), server_default='false', nullable=False))
    op.drop_index(op.f('ix_outfits_user_id'), table_name='outfits')
    op.create_index('ix_outfits_user_favorite', 'outfits', ['user_id', 'favorite'], unique=False)
    # Rows that predate suggestions were saved on purpose. Normalize them so the unique
    # constraint can be added: sort item_ids, then drop duplicates (keeping the oldest).
    op.execute("UPDATE outfits SET favorite = true")
    op.execute("UPDATE outfits SET item_ids = ARRAY(SELECT unnest(item_ids) ORDER BY 1)")
    op.execute(
        "DELETE FROM outfits a USING outfits b "
        "WHERE a.id > b.id AND a.user_id = b.user_id AND a.item_ids = b.item_ids"
    )
    op.create_unique_constraint('uq_outfits_user_items', 'outfits', ['user_id', 'item_ids'])


def downgrade() -> None:
    op.drop_constraint('uq_outfits_user_items', 'outfits', type_='unique')
    op.drop_index('ix_outfits_user_favorite', table_name='outfits')
    op.create_index(op.f('ix_outfits_user_id'), 'outfits', ['user_id'], unique=False)
    op.drop_column('outfits', 'favorite')
    op.drop_column('outfits', 'breakdown')
    op.drop_column('outfits', 'context')
