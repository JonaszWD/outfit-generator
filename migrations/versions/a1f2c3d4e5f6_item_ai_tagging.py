"""item ai tagging: cutout, status, ai tags

Revision ID: a1f2c3d4e5f6
Revises: 389d068f6508
Create Date: 2026-10-06 10:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a1f2c3d4e5f6"
down_revision: str | None = "389d068f6508"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

tagging_status = postgresql.ENUM(
    "pending", "processing", "done", "failed", name="tagging_status", create_type=False
)


def upgrade() -> None:
    tagging_status.create(op.get_bind(), checkfirst=True)
    op.add_column("items", sa.Column("original_image_path", sa.String(), nullable=True))
    op.add_column("items", sa.Column("cutout_image_path", sa.String(), nullable=True))
    op.add_column(
        "items",
        sa.Column("tagging_status", tagging_status, server_default="pending", nullable=False),
    )
    op.add_column("items", sa.Column("tagging_error", sa.Text(), nullable=True))
    op.add_column(
        "items",
        sa.Column("tags_confirmed", sa.Boolean(), server_default="false", nullable=False),
    )
    op.add_column("items", sa.Column("ai_tags", postgresql.JSONB(), nullable=True))
    op.add_column("items", sa.Column("ai_confidence", postgresql.JSONB(), nullable=True))
    # New items are tagged by the pipeline and may have no category yet.
    op.alter_column(
        "items",
        "category",
        existing_type=postgresql.ENUM(name="item_category", create_type=False),
        nullable=True,
    )
    # Existing rows were hand-tagged in Phase 1: they are finished and confirmed.
    op.execute(
        "UPDATE items SET original_image_path = image_path, "
        "tagging_status = 'done', tags_confirmed = true"
    )


def downgrade() -> None:
    # Untagged rows cannot satisfy NOT NULL; give them a placeholder so the downgrade runs.
    op.execute("UPDATE items SET category = 'top' WHERE category IS NULL")
    op.alter_column(
        "items",
        "category",
        existing_type=postgresql.ENUM(name="item_category", create_type=False),
        nullable=False,
    )
    for column in (
        "ai_confidence",
        "ai_tags",
        "tags_confirmed",
        "tagging_error",
        "tagging_status",
        "cutout_image_path",
        "original_image_path",
    ):
        op.drop_column("items", column)
    tagging_status.drop(op.get_bind(), checkfirst=True)
