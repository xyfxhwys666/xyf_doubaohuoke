from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class CollectionTask(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "collection_tasks"

    name: Mapped[str] = mapped_column(String(160), nullable=False)
    request_text: Mapped[str] = mapped_column(Text, nullable=False)
    cities: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    all_cities: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    keywords: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    providers: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    models: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    required_contacts: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    contact_match: Mapped[str] = mapped_column(String(16), nullable=False, default="all")
    limit_per_city: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft", index=True)
    collection_phase: Mapped[str] = mapped_column(String(32), nullable=False, default="discovering")
    total_units: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_units: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lead_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_unit_keys: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    unit_errors: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    empty_units: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    verification_errors: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    verification_state: Mapped[str] = mapped_column(String(32), nullable=False, default="not_started")
    candidate_target: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    enrichment_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    enrichment_completed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_city_discovery: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    created_by: Mapped[str | None] = mapped_column(String(120))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(120))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    use_doubao_web_chat: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    web_chat_status: Mapped[str] = mapped_column(String(32), nullable=False, default="disabled")
    web_chat_error: Mapped[str | None] = mapped_column(Text)
    web_chat_current_company: Mapped[str | None] = mapped_column(String(320))
    web_chat_checkpoint: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    leads: Mapped[list["CollectionLead"]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )
    city_progress: Mapped[list["CollectionCityProgress"]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )
    activity: Mapped[list["CollectionActivity"]] = relationship(
        back_populates="task", cascade="all, delete-orphan", order_by="CollectionActivity.sequence"
    )


class CollectionLead(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "collection_leads"
    __table_args__ = (
        UniqueConstraint("task_id", "city", "normalized_company_name", name="uq_collection_lead_company"),
    )

    task_id: Mapped[UUID] = mapped_column(
        ForeignKey("collection_tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    city: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    company_name: Mapped[str] = mapped_column(String(320), nullable=False)
    normalized_company_name: Mapped[str] = mapped_column(String(320), nullable=False)
    mobile: Mapped[str | None] = mapped_column(String(32), index=True)
    contact_person_name: Mapped[str | None] = mapped_column(String(160))
    contact_person_role: Mapped[str | None] = mapped_column(String(160))
    contact_verification_status: Mapped[str] = mapped_column(
        String(64), nullable=False, default="unverified"
    )
    contact_evidence_url: Mapped[str | None] = mapped_column(Text)
    contact_evidence_quote: Mapped[str | None] = mapped_column(Text)
    contact_evidence_urls: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    contact_evidence_quotes: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    contact_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    contact_verification_reason: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(String(320), index=True)
    wechat: Mapped[str | None] = mapped_column(String(128), index=True)
    address: Mapped[str | None] = mapped_column(Text)
    service_intro: Mapped[str | None] = mapped_column(Text)
    saved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    discovery_channel: Mapped[str | None] = mapped_column(String(64))
    source_url: Mapped[str | None] = mapped_column(Text)
    matched_keyword: Mapped[str | None] = mapped_column(String(160))
    source_confidence: Mapped[int | None] = mapped_column(Integer)
    confidence_score: Mapped[int | None] = mapped_column(Integer)
    confidence_level: Mapped[str | None] = mapped_column(String(32))
    verification_status: Mapped[str | None] = mapped_column(String(64))
    verification_models: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    verification_detail: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    company_name_verification_status: Mapped[str] = mapped_column(
        String(64), nullable=False, default="unverified"
    )
    company_name_evidence_urls: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list
    )
    company_name_verification_reason: Mapped[str | None] = mapped_column(Text)
    company_name_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    company_name_verification_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    candidate_reason: Mapped[str | None] = mapped_column(Text)
    enrichment_state: Mapped[str | None] = mapped_column(String(32))
    enrichment_model: Mapped[str | None] = mapped_column(String(64))
    enrichment_error: Mapped[str | None] = mapped_column(Text)

    task: Mapped[CollectionTask] = relationship(back_populates="leads")


class CollectionCityProgress(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "collection_city_progress"
    __table_args__ = (UniqueConstraint("task_id", "city", name="uq_collection_city_progress"),)

    task_id: Mapped[UUID] = mapped_column(
        ForeignKey("collection_tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    city: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", index=True)
    total_units: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_units: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lead_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    candidate_target: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    enrichment_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    enrichment_completed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    task: Mapped[CollectionTask] = relationship(back_populates="city_progress")


class CollectionActivity(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "collection_activity"
    __table_args__ = (UniqueConstraint("task_id", "sequence", name="uq_collection_activity_sequence"),)

    task_id: Mapped[UUID] = mapped_column(
        ForeignKey("collection_tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String(120), nullable=False)
    actor: Mapped[str | None] = mapped_column(String(120))
    city: Mapped[str | None] = mapped_column(String(100), index=True)
    keyword: Mapped[str | None] = mapped_column(String(160))
    provider: Mapped[str | None] = mapped_column(String(64))
    outcome: Mapped[str | None] = mapped_column(String(32))
    detail: Mapped[str | None] = mapped_column(Text)

    task: Mapped[CollectionTask] = relationship(back_populates="activity")
