"""Add ``state`` column to sessions.

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-07-06 12:00:00.000000

Ports the ``Session.state`` column introduced upstream in tiled 0.2.10.  The
column stores a JSON dict populated by an authenticator's
``UserSessionState.state`` (e.g. upstream OIDC access/refresh tokens carried
across refresh_session calls) so downstream services that share
bluesky-httpserver authentication can retrieve them via Tiled access tokens.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "b2c3d4e5f6a7"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade():
    """Add sessions.state JSON column with default ``{}``."""
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.add_column(
            sa.Column(
                "state",
                sa.JSON(),
                nullable=False,
                server_default="{}",
            )
        )


def downgrade():
    """Drop sessions.state column."""
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.drop_column("state")
