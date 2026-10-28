"""Create normalized collection storage tables.

Revision ID: 0002_collection_storage
Revises: 0001_identity
Create Date: 2026-07-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_collection_storage"
down_revision: str | None = "0001_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "collection_tasks",
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("request_text", sa.Text(), nullable=False),
        sa.Column("cities", sa.JSON(), nullable=False),
        sa.Column("all_cities", sa.Boolean(), nullable=False),
        sa.Column("keywords", sa.JSON(), nullable=False),
        sa.Column("providers", sa.JSON(), nullable=False),
        sa.Column("models", sa.JSON(), nullable=False),
        sa.Column("required_contacts", sa.JSON(), nullable=False),
        sa.Column("contact_match", sa.String(length=16), nullable=False),
        sa.Column("limit_per_city", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("collection_phase", sa.String(length=32), nullable=False),
        sa.Column("total_units", sa.Integer(), nullable=False),
        sa.Column("completed_units", sa.Integer(), nullable=False),
        sa.Column("lead_count", sa.Integer(), nullable=False),
        sa.Column("completed_unit_keys", sa.JSON(), nullable=False),
        sa.Column("unit_errors", sa.Integer(), nullable=False),
        sa.Column("empty_units", sa.Integer(), nullable=False),
        sa.Column("verification_errors", sa.Integer(), nullable=False),
        sa.Column("verification_state", sa.String(length=32), nullable=False),
        sa.Column("candidate_target", sa.Integer(), nullable=False),
        sa.Column("candidate_count", sa.Integer(), nullable=False),
        sa.Column("enrichment_total", sa.Integer(), nullable=False),
        sa.Column("enrichment_completed", sa.Integer(), nullable=False),
        sa.Column("completed_city_discovery", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.String(length=120), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_collection_tasks_lease_expires_at"), "collection_tasks", ["lease_expires_at"])
    op.create_index(op.f("ix_collection_tasks_status"), "collection_tasks", ["status"])

    op.create_table(
        "collection_leads",
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("city", sa.String(length=100), nullable=False),
        sa.Column("company_name", sa.String(length=320), nullable=False),
        sa.Column("normalized_company_name", sa.String(length=320), nullable=False),
        sa.Column("mobile", sa.String(length=32), nullable=True),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("wechat", sa.String(length=128), nullable=True),
        sa.Column("address", sa.Text(), nullable=True),
        sa.Column("service_intro", sa.Text(), nullable=True),
        sa.Column("saved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("discovery_channel", sa.String(length=64), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("matched_keyword", sa.String(length=160), nullable=True),
        sa.Column("source_confidence", sa.Integer(), nullable=True),
        sa.Column("confidence_score", sa.Integer(), nullable=True),
        sa.Column("confidence_level", sa.String(length=32), nullable=True),
        sa.Column("verification_status", sa.String(length=64), nullable=True),
        sa.Column("verification_models", sa.JSON(), nullable=False),
        sa.Column("verification_detail", sa.JSON(), nullable=False),
        sa.Column("candidate_reason", sa.Text(), nullable=True),
        sa.Column("enrichment_state", sa.String(length=32), nullable=True),
        sa.Column("enrichment_model", sa.String(length=64), nullable=True),
        sa.Column("enrichment_error", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["collection_tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", "city", "normalized_company_name", name="uq_collection_lead_company"),
    )
    op.create_index(op.f("ix_collection_leads_city"), "collection_leads", ["city"])
    op.create_index(op.f("ix_collection_leads_email"), "collection_leads", ["email"])
    op.create_index(op.f("ix_collection_leads_mobile"), "collection_leads", ["mobile"])
    op.create_index(op.f("ix_collection_leads_task_id"), "collection_leads", ["task_id"])
    op.create_index(op.f("ix_collection_leads_wechat"), "collection_leads", ["wechat"])

    op.create_table(
        "collection_city_progress",
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("city", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("total_units", sa.Integer(), nullable=False),
        sa.Column("completed_units", sa.Integer(), nullable=False),
        sa.Column("lead_count", sa.Integer(), nullable=False),
        sa.Column("candidate_target", sa.Integer(), nullable=False),
        sa.Column("candidate_count", sa.Integer(), nullable=False),
        sa.Column("enrichment_total", sa.Integer(), nullable=False),
        sa.Column("enrichment_completed", sa.Integer(), nullable=False),
        sa.Column("error_count", sa.Integer(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["collection_tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", "city", name="uq_collection_city_progress"),
    )
    op.create_index(op.f("ix_collection_city_progress_city"), "collection_city_progress", ["city"])
    op.create_index(op.f("ix_collection_city_progress_status"), "collection_city_progress", ["status"])
    op.create_index(op.f("ix_collection_city_progress_task_id"), "collection_city_progress", ["task_id"])

    op.create_table(
        "collection_activity",
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=120), nullable=False),
        sa.Column("actor", sa.String(length=120), nullable=True),
        sa.Column("city", sa.String(length=100), nullable=True),
        sa.Column("keyword", sa.String(length=160), nullable=True),
        sa.Column("provider", sa.String(length=64), nullable=True),
        sa.Column("outcome", sa.String(length=32), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["collection_tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", "sequence", name="uq_collection_activity_sequence"),
    )
    op.create_index(op.f("ix_collection_activity_city"), "collection_activity", ["city"])
    op.create_index(op.f("ix_collection_activity_task_id"), "collection_activity", ["task_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_collection_activity_task_id"), table_name="collection_activity")
    op.drop_index(op.f("ix_collection_activity_city"), table_name="collection_activity")
    op.drop_table("collection_activity")
    op.drop_index(op.f("ix_collection_city_progress_task_id"), table_name="collection_city_progress")
    op.drop_index(op.f("ix_collection_city_progress_status"), table_name="collection_city_progress")
    op.drop_index(op.f("ix_collection_city_progress_city"), table_name="collection_city_progress")
    op.drop_table("collection_city_progress")
    op.drop_index(op.f("ix_collection_leads_wechat"), table_name="collection_leads")
    op.drop_index(op.f("ix_collection_leads_task_id"), table_name="collection_leads")
    op.drop_index(op.f("ix_collection_leads_mobile"), table_name="collection_leads")
    op.drop_index(op.f("ix_collection_leads_email"), table_name="collection_leads")
    op.drop_index(op.f("ix_collection_leads_city"), table_name="collection_leads")
    op.drop_table("collection_leads")
    op.drop_index(op.f("ix_collection_tasks_status"), table_name="collection_tasks")
    op.drop_index(op.f("ix_collection_tasks_lease_expires_at"), table_name="collection_tasks")
    op.drop_table("collection_tasks")
