"""Add Doubao web-chat collection state and multi-source contact evidence.

Revision ID: 0004_doubao_web_chat
Revises: 0003_contact_evidence
Create Date: 2026-07-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_doubao_web_chat"
down_revision: str | None = "0003_contact_evidence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "collection_tasks",
        sa.Column("use_doubao_web_chat", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column(
        "collection_tasks",
        sa.Column("web_chat_status", sa.String(length=32), server_default="disabled", nullable=False),
    )
    op.add_column("collection_tasks", sa.Column("web_chat_error", sa.Text(), nullable=True))
    op.add_column(
        "collection_tasks",
        sa.Column("web_chat_current_company", sa.String(length=320), nullable=True),
    )
    op.add_column(
        "collection_tasks",
        sa.Column("web_chat_checkpoint", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
    )
    op.add_column(
        "collection_leads",
        sa.Column("contact_evidence_urls", sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
    )
    op.add_column(
        "collection_leads",
        sa.Column("contact_evidence_quotes", sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
    )

    op.execute("UPDATE collection_tasks SET use_doubao_web_chat = false WHERE use_doubao_web_chat IS NULL")
    op.execute("UPDATE collection_tasks SET web_chat_status = 'disabled' WHERE web_chat_status IS NULL")
    op.execute("UPDATE collection_tasks SET web_chat_checkpoint = '{}' WHERE web_chat_checkpoint IS NULL")
    op.execute("UPDATE collection_leads SET contact_evidence_urls = '[]' WHERE contact_evidence_urls IS NULL")
    op.execute("UPDATE collection_leads SET contact_evidence_quotes = '[]' WHERE contact_evidence_quotes IS NULL")

    op.alter_column("collection_tasks", "web_chat_status", server_default=None)
    op.alter_column("collection_tasks", "web_chat_checkpoint", server_default=None)
    op.alter_column("collection_leads", "contact_evidence_urls", server_default=None)
    op.alter_column("collection_leads", "contact_evidence_quotes", server_default=None)


def downgrade() -> None:
    op.drop_column("collection_leads", "contact_evidence_quotes")
    op.drop_column("collection_leads", "contact_evidence_urls")
    op.drop_column("collection_tasks", "web_chat_checkpoint")
    op.drop_column("collection_tasks", "web_chat_current_company")
    op.drop_column("collection_tasks", "web_chat_error")
    op.drop_column("collection_tasks", "web_chat_status")
    op.drop_column("collection_tasks", "use_doubao_web_chat")
