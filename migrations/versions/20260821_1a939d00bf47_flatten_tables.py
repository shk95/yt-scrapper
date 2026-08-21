"""flatten tables

Revision ID: 1a939d00bf47
Revises: 55a24ac7a270
Created: 2026-08-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "1a939d00bf47"
down_revision: str | None = "55a24ac7a270"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "video_snapshots",
        sa.Column("artifact_id", sa.String(length=32), nullable=False),
        sa.Column("video_id", sa.String(length=500), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=True),
        sa.Column("channel_id", sa.String(length=500), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("view_count", sa.BigInteger(), nullable=True),
        sa.Column("like_count", sa.BigInteger(), nullable=True),
        sa.Column("comment_count", sa.BigInteger(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_date", sa.Date(), nullable=True),
        sa.PrimaryKeyConstraint("artifact_id"),
    )
    op.create_index(
        "ix_video_snapshot_series", "video_snapshots", ["video_id", "fetched_at"], unique=False
    )

    op.create_table(
        "listing_entries",
        sa.Column("artifact_id", sa.String(length=32), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("target", sa.String(length=500), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("video_id", sa.String(length=500), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("view_count", sa.BigInteger(), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("channel", sa.Text(), nullable=True),
        sa.Column("channel_id", sa.String(length=500), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("artifact_id", "position"),
    )
    op.create_index(
        "ix_listing_entry_series", "listing_entries", ["target", "fetched_at"], unique=False
    )
    op.create_index("ix_listing_entry_video", "listing_entries", ["video_id"], unique=False)

    op.create_table(
        "channel_snapshots",
        sa.Column("artifact_id", sa.String(length=32), nullable=False),
        sa.Column("channel_id", sa.String(length=500), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("handle", sa.String(length=500), nullable=True),
        sa.Column("subscriber_count_approximate", sa.BigInteger(), nullable=True),
        sa.Column("view_count", sa.BigInteger(), nullable=True),
        sa.Column("video_count", sa.Integer(), nullable=True),
        sa.Column("country", sa.String(length=100), nullable=True),
        sa.PrimaryKeyConstraint("artifact_id"),
    )
    op.create_index(
        "ix_channel_snapshot_series",
        "channel_snapshots",
        ["channel_id", "fetched_at"],
        unique=False,
    )

    op.create_table(
        "comments",
        sa.Column("video_id", sa.String(length=500), nullable=False),
        sa.Column("comment_id", sa.String(length=200), nullable=False),
        sa.Column("parent_id", sa.String(length=200), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("author", sa.Text(), nullable=True),
        sa.Column("author_id", sa.String(length=500), nullable=True),
        sa.Column("like_count", sa.BigInteger(), nullable=True),
        sa.Column("is_hearted_by_uploader", sa.Boolean(), nullable=False),
        sa.Column("is_pinned", sa.Boolean(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("video_id", "comment_id"),
    )
    op.create_index("ix_comment_published", "comments", ["video_id", "published_at"], unique=False)

    op.create_table(
        "transcripts",
        sa.Column("video_id", sa.String(length=500), nullable=False),
        sa.Column("language", sa.String(length=64), nullable=False),
        sa.Column("is_automatic", sa.Boolean(), nullable=False),
        sa.Column("full_text", sa.Text(), nullable=False),
        sa.Column("segment_count", sa.Integer(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("video_id", "language"),
    )

    op.create_table(
        "flatten_progress",
        sa.Column("identifier", sa.String(length=32), nullable=False),
        sa.Column("cursor_fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cursor_identifier", sa.String(length=32), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("identifier"),
    )


def downgrade() -> None:
    op.drop_table("flatten_progress")
    op.drop_table("transcripts")
    op.drop_index("ix_comment_published", table_name="comments")
    op.drop_table("comments")
    op.drop_index("ix_channel_snapshot_series", table_name="channel_snapshots")
    op.drop_table("channel_snapshots")
    op.drop_index("ix_listing_entry_video", table_name="listing_entries")
    op.drop_index("ix_listing_entry_series", table_name="listing_entries")
    op.drop_table("listing_entries")
    op.drop_index("ix_video_snapshot_series", table_name="video_snapshots")
    op.drop_table("video_snapshots")
