from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.base import Base
from app.models.collection import CollectionActivity, CollectionCityProgress, CollectionLead, CollectionTask


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as database_session:
        yield database_session
    Base.metadata.drop_all(engine)
    engine.dispose()


def test_collection_task_persists_configuration_progress_and_related_leads(session: Session) -> None:
    task = CollectionTask(
        id=uuid4(),
        name="广州采集",
        request_text="找软装公司",
        cities=["广州市"],
        all_cities=True,
        keywords=["软装公司"],
        models=["doubao"],
        required_contacts=["mobile"],
        status="running",
        total_units=1,
        completed_units=0,
        heartbeat_at=datetime.now(UTC),
        lease_owner="worker-1",
        lease_expires_at=datetime.now(UTC),
    )
    task.leads.append(
        CollectionLead(
            city="广州市",
            company_name="广州甲公司",
            normalized_company_name="广州甲公司",
            mobile="13800138000",
            contact_person_name="张三",
            contact_person_role="业务负责人",
            contact_verification_status="verified_public_web",
            contact_evidence_url="https://example.test/contact",
            contact_evidence_quote="广州甲公司 张三 13800138000",
            contact_verified_at=datetime.now(UTC),
            contact_verification_reason="同页证据通过",
            email="sales@example.test",
            wechat="guangzhoujia",
            company_name_verification_status="verified_public_web",
            company_name_evidence_urls=["https://example.test/company"],
            company_name_verification_reason="公开网页已确认",
            company_name_verified_at=datetime.now(UTC),
            company_name_verification_attempts=1,
        )
    )
    task.city_progress.append(CollectionCityProgress(city="广州市", status="running"))
    task.activity.append(CollectionActivity(sequence=1, action="created"))

    session.add(task)
    session.commit()

    loaded = session.get(CollectionTask, task.id)
    assert loaded is not None
    assert loaded.cities == ["广州市"]
    assert loaded.all_cities is True
    assert loaded.keywords == ["软装公司"]
    assert loaded.models == ["doubao"]
    assert loaded.required_contacts == ["mobile"]
    assert loaded.leads[0].mobile == "13800138000"
    assert loaded.leads[0].contact_person_name == "张三"
    assert loaded.leads[0].contact_person_role == "业务负责人"
    assert loaded.leads[0].contact_verification_status == "verified_public_web"
    assert loaded.leads[0].contact_evidence_url == "https://example.test/contact"
    assert loaded.leads[0].contact_evidence_quote == "广州甲公司 张三 13800138000"
    assert loaded.leads[0].contact_verified_at is not None
    assert loaded.leads[0].contact_verification_reason == "同页证据通过"
    assert loaded.leads[0].company_name_verification_status == "verified_public_web"
    assert loaded.leads[0].company_name_evidence_urls == ["https://example.test/company"]
    assert loaded.leads[0].company_name_verification_attempts == 1
    assert loaded.city_progress[0].city == "广州市"
    assert loaded.activity[0].action == "created"

    indexes = inspect(session.bind).get_indexes("collection_leads")
    assert {"mobile", "email", "wechat"} <= {column for index in indexes for column in index["column_names"]}


def test_collection_lead_is_unique_by_task_city_and_normalized_name(session: Session) -> None:
    task = CollectionTask(id=uuid4(), name="广州采集", request_text="找软装公司", status="draft")
    session.add(task)
    session.flush()
    session.add(
        CollectionLead(
            task_id=task.id,
            city="广州市",
            company_name="广州甲公司",
            normalized_company_name="广州甲公司",
        )
    )
    session.commit()
    session.add(
        CollectionLead(
            task_id=task.id,
            city="广州市",
            company_name="广州甲公司",
            normalized_company_name="广州甲公司",
        )
    )

    with pytest.raises(IntegrityError):
        session.commit()


def test_collection_lead_defaults_contact_evidence_to_unverified(session: Session) -> None:
    task = CollectionTask(id=uuid4(), name="广州采集", request_text="找软装公司", status="draft")
    task.leads.append(
        CollectionLead(
            city="广州市",
            company_name="广州乙公司",
            normalized_company_name="广州乙公司",
        )
    )
    session.add(task)
    session.commit()

    assert task.leads[0].contact_verification_status == "unverified"
    assert task.leads[0].contact_evidence_url is None


def test_collection_task_and_lead_default_web_chat_and_evidence_lists(session: Session) -> None:
    task = CollectionTask(id=uuid4(), name="广州采集", request_text="找软装公司", status="draft")
    task.leads.append(
        CollectionLead(
            city="广州市",
            company_name="广州丙公司",
            normalized_company_name="广州丙公司",
        )
    )
    session.add(task)
    session.commit()

    assert task.use_doubao_web_chat is False
    assert task.web_chat_status == "disabled"
    assert task.web_chat_error is None
    assert task.web_chat_current_company is None
    assert task.web_chat_checkpoint == {}
    assert task.leads[0].contact_evidence_urls == []
    assert task.leads[0].contact_evidence_quotes == []


def test_collection_city_progress_is_unique_by_task_and_city(session: Session) -> None:
    task = CollectionTask(id=uuid4(), name="广州采集", request_text="找软装公司", status="draft")
    session.add(task)
    session.flush()
    session.add(CollectionCityProgress(task_id=task.id, city="广州市", status="pending"))
    session.commit()
    session.add(CollectionCityProgress(task_id=task.id, city="广州市", status="completed"))

    with pytest.raises(IntegrityError):
        session.commit()


def test_collection_activity_is_unique_by_task_and_sequence(session: Session) -> None:
    task = CollectionTask(id=uuid4(), name="广州采集", request_text="找软装公司", status="draft")
    session.add(task)
    session.flush()
    session.add(CollectionActivity(task_id=task.id, sequence=1, action="created"))
    session.commit()
    session.add(CollectionActivity(task_id=task.id, sequence=1, action="started"))

    with pytest.raises(IntegrityError):
        session.commit()
