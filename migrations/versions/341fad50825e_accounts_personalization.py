"""accounts personalization

Revision ID: 341fad50825e
Revises: da8366163587
Create Date: 2026-10-06 17:47:50.506278
"""
from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '341fad50825e'
down_revision: str | None = 'da8366163587'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


item_availability = postgresql.ENUM(
    "available", "laundry", "unavailable", name="item_availability", create_type=False
)


def upgrade() -> None:
    item_availability.create(op.get_bind(), checkfirst=True)
    op.create_table('auth_attempts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('kind', sqlmodel.sql.sqltypes.AutoString(length=20), nullable=False),
    sa.Column('key', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_auth_attempts_lookup', 'auth_attempts', ['kind', 'key', 'created_at'], unique=False)
    op.create_table('outfit_feedback',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('item_ids', postgresql.ARRAY(sa.Integer()), nullable=False),
    sa.Column('rating', sa.SmallInteger(), nullable=False),
    sa.Column('reason', sqlmodel.sql.sqltypes.AutoString(length=40), nullable=True),
    sa.Column('rules', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'item_ids', name='uq_feedback_user_items')
    )
    op.create_index(op.f('ix_outfit_feedback_user_id'), 'outfit_feedback', ['user_id'], unique=False)
    op.create_table('password_resets',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('token_hash', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('token_hash')
    )
    op.create_index(op.f('ix_password_resets_user_id'), 'password_resets', ['user_id'], unique=False)
    op.create_table('sessions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('token_hash', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('token_hash')
    )
    op.create_index(op.f('ix_sessions_user_id'), 'sessions', ['user_id'], unique=False)
    op.create_table('user_preferences',
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('home_city', sqlmodel.sql.sqltypes.AutoString(length=120), nullable=True),
    sa.Column('latitude', sa.Float(), nullable=True),
    sa.Column('longitude', sa.Float(), nullable=True),
    sa.Column('style_tags', postgresql.ARRAY(sa.Text()), server_default='{}', nullable=False),
    sa.Column('color_strictness', sa.Float(), server_default='0.5', nullable=False),
    sa.Column('formality_bias', sa.Float(), server_default='0', nullable=False),
    sa.Column('units', sqlmodel.sql.sqltypes.AutoString(length=10), server_default='metric', nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('user_id')
    )
    op.create_table('planned_outfits',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('day', sa.Date(), nullable=False),
    sa.Column('outfit_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['outfit_id'], ['outfits.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'day', name='uq_planned_user_day')
    )
    op.create_index(op.f('ix_planned_outfits_user_id'), 'planned_outfits', ['user_id'], unique=False)
    op.add_column('items', sa.Column('availability', item_availability, server_default='available', nullable=False))
    op.add_column('items', sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('outfits', sa.Column('occasions', postgresql.ARRAY(sa.Text()), server_default='{}', nullable=False))


def downgrade() -> None:
    op.drop_column('outfits', 'occasions')
    op.drop_column('items', 'archived_at')
    op.drop_column('items', 'availability')
    op.drop_index(op.f('ix_planned_outfits_user_id'), table_name='planned_outfits')
    op.drop_table('planned_outfits')
    op.drop_table('user_preferences')
    op.drop_index(op.f('ix_sessions_user_id'), table_name='sessions')
    op.drop_table('sessions')
    op.drop_index(op.f('ix_password_resets_user_id'), table_name='password_resets')
    op.drop_table('password_resets')
    op.drop_index(op.f('ix_outfit_feedback_user_id'), table_name='outfit_feedback')
    op.drop_table('outfit_feedback')
    op.drop_index('ix_auth_attempts_lookup', table_name='auth_attempts')
    op.drop_table('auth_attempts')
    sa.Enum(name='item_availability').drop(op.get_bind(), checkfirst=True)
