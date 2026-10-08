"""candidate analysis

Revision ID: da8366163587
Revises: 9398670348ef
Create Date: 2026-10-06 16:44:48.575391
"""
from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'da8366163587'
down_revision: str | None = '9398670348ef'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


candidate_status = postgresql.ENUM(
    "pending", "processing", "done", "failed", name="candidate_status", create_type=False
)


def upgrade() -> None:
    candidate_status.create(op.get_bind(), checkfirst=True)
    op.add_column('candidates', sa.Column('url_hash', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=True))
    op.add_column('candidates', sa.Column('status', candidate_status, server_default='pending', nullable=False))
    op.add_column('candidates', sa.Column('error', sa.Text(), nullable=True))
    op.add_column('candidates', sa.Column('product_info', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column('candidates', sa.Column('result', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column('candidates', sa.Column('analyzed_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('candidates', sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False))
    op.alter_column('candidates', 'url',
               existing_type=sa.VARCHAR(),
               type_=sa.Text(),
               nullable=True)
    op.alter_column('candidates', 'image_url',
               existing_type=sa.VARCHAR(),
               type_=sa.Text(),
               existing_nullable=True)
    # Rows that predate analysis are finished; hash their links so the new unique key holds.
    op.execute("UPDATE candidates SET status = 'done'")
    op.execute(
        "UPDATE candidates SET url_hash = encode(sha256(convert_to(url, 'UTF8')), 'hex') "
        "WHERE url IS NOT NULL"
    )
    op.execute(
        "DELETE FROM candidates a USING candidates b WHERE a.id > b.id "
        "AND a.user_id = b.user_id AND a.url_hash = b.url_hash"
    )
    op.drop_constraint(op.f('uq_candidates_user_url'), 'candidates', type_='unique')
    op.create_index(op.f('ix_candidates_url_hash'), 'candidates', ['url_hash'], unique=False)
    op.create_unique_constraint('uq_candidates_user_urlhash', 'candidates', ['user_id', 'url_hash'])


def downgrade() -> None:
    # Photo-upload candidates have no link, which the old schema cannot represent.
    op.execute("DELETE FROM candidates WHERE url IS NULL")
    op.drop_constraint('uq_candidates_user_urlhash', 'candidates', type_='unique')
    op.drop_index(op.f('ix_candidates_url_hash'), table_name='candidates')
    op.create_unique_constraint(op.f('uq_candidates_user_url'), 'candidates', ['user_id', 'url'], postgresql_nulls_not_distinct=False)
    op.alter_column('candidates', 'image_url',
               existing_type=sa.Text(),
               type_=sa.VARCHAR(),
               existing_nullable=True)
    op.alter_column('candidates', 'url',
               existing_type=sa.Text(),
               type_=sa.VARCHAR(),
               nullable=False)
    op.drop_column('candidates', 'updated_at')
    op.drop_column('candidates', 'analyzed_at')
    op.drop_column('candidates', 'result')
    op.drop_column('candidates', 'product_info')
    op.drop_column('candidates', 'error')
    op.drop_column('candidates', 'status')
    op.drop_column('candidates', 'url_hash')
    sa.Enum(name='candidate_status').drop(op.get_bind(), checkfirst=True)
