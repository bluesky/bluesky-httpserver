"""Add error column to pending_sessions table.

Revision ID: b7c8d9e0f1a2
Revises: a1b2c3d4e5f6
Create Date: 2026-08-24 12:00:00.000000

Adds a nullable `error` column to pending_sessions so that browser-side
authentication failures can be signalled to the polling CLI client
immediately, rather than waiting for the pending session to expire.

Possible values:
  NULL              - still pending
  "access_denied"   - OIDC provider denied / user cancelled
  "unauthorized_user" - user authenticated but not permitted on this server
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "b7c8d9e0f1a2"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("pending_sessions") as batch_op:
        batch_op.add_column(sa.Column("error", sa.Unicode(64), nullable=True))


def downgrade():
    with op.batch_alter_table("pending_sessions") as batch_op:
        batch_op.drop_column("error")
