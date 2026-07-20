"""Add PendingSession table for device code flow.

Revision ID: a1b2c3d4e5f6
Revises: 722ff4e4fcc7
Create Date: 2026-02-13 12:00:00.000000

"""

from alembic import op
from sqlalchemy import Column, DateTime, ForeignKey, Integer, LargeBinary, Unicode
from sqlalchemy.sql import func

# revision identifiers, used by Alembic.
revision = "a1b2c3d4e5f6"
down_revision = "722ff4e4fcc7"
branch_labels = None
depends_on = None


def upgrade():
    """
    Add pending_sessions table for device code flow authentication.
    """
    op.create_table(
        "pending_sessions",
        Column("time_created", DateTime(timezone=False), server_default=func.now()),
        Column("time_updated", DateTime(timezone=False), onupdate=func.now()),
        Column(
            "hashed_device_code",
            LargeBinary(32),
            primary_key=True,
            index=True,
            nullable=False,
        ),
        Column(
            "user_code",
            Unicode(8),
            index=True,
            nullable=False,
        ),
        Column(
            "expiration_time",
            DateTime(timezone=False),
            nullable=False,
        ),
        Column(
            "session_id",
            Integer,
            ForeignKey("sessions.id"),
            nullable=True,
        ),
    )


def downgrade():
    """
    Remove pending_sessions table.
    """
    op.drop_table("pending_sessions")
