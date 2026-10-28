"""Add public contact evidence fields.

Revision ID: 0003_contact_evidence
Revises: 0002_collection_storage
Create Date: 2026-07-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_contact_evidence"
down_revision: str | None = "0002_collection_storage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "collection_leads",
        sa.Column("contact_person_name", sa.String(length=160), nullable=True),
    )
    op.add_column(
        "collection_leads",
        sa.Column("contact_person_role", sa.String(length=160), nullable=True),
    )
    op.add_column(
        "collection_leads",
        sa.Column(
            "contact_verification_status",
            sa.String(length=64),
            server_default="unverified",
            nullable=False,
        ),
    )
    op.add_column(
        "collection_leads",
        sa.Column("contact_evidence_url", sa.Text(), nullable=True),
    )
    op.add_column(
        "collection_leads",
        sa.Column("contact_evidence_quote", sa.Text(), nullable=True),
    )
    op.add_column(
        "collection_leads",
        sa.Column("contact_verified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "collection_leads",
        sa.Column("contact_verification_reason", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("collection_leads", "contact_verification_reason")
    op.drop_column("collection_leads", "contact_verified_at")
    op.drop_column("collection_leads", "contact_evidence_quote")
    op.drop_column("collection_leads", "contact_evidence_url")
    op.drop_column("collection_leads", "contact_verification_status")
    op.drop_column("collection_leads", "contact_person_role")
    op.drop_column("collection_leads", "contact_person_name")
