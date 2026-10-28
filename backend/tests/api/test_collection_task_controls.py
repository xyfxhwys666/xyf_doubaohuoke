from collections.abc import Generator
from io import BytesIO
from secrets import token_urlsafe
from threading import Event, Thread
from zipfile import ZipFile

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


class FakeWebSearchProvider:
    id = "web_search"
    label = "Public web search"
    configured = True

    def list_cities(self) -> list[str]:
        return ["武汉市", "长沙市"]

    def collect(self, *, city: str, keyword: str, request_text: str) -> list[dict]:
        return []


class FakeVerifier:
    id = "deepseek"
    label = "DeepSeek"
    configured = True


@pytest.fixture
def collection_client(tmp_path) -> Generator[TestClient, None, None]:
    settings = Settings(
        app_env="test",
        database_url="sqlite+pysqlite:///:memory:",
        app_master_key=Fernet.generate_key().decode("ascii"),
        session_secret=token_urlsafe(32),
        data_dir=tmp_path,
        erp_auto_push=False,
    )
    client = TestClient(create_app(settings))
    client.app.state.collection_service.providers = {"web_search": FakeWebSearchProvider()}
    client.app.state.collection_service.verifiers = {"deepseek": FakeVerifier()}
    try:
        yield client
    finally:
        client.app.state.collection_service.shutdown()
        client.close()


def create_natural_language_task(client: TestClient, request_text: str) -> dict:
    response = client.post(
        "/api/v1/collection-tasks/from-natural-language",
        json={"request_text": request_text, "models": ["deepseek"]},
    )

    assert response.status_code == 201
    return response.json()


def direct_task_payload(**overrides) -> dict:
    return {
        "name": "武汉软装客户",
        "request_text": "找武汉软装公司",
        "cities": ["武汉市"],
        "all_cities": False,
        "keywords": ["软装公司"],
        "providers": ["web_search"],
        "models": ["deepseek"],
        "required_contacts": ["mobile"],
        **overrides,
    }


def test_direct_task_saves_explicit_auto_delivery_false(
    collection_client: TestClient,
) -> None:
    response = collection_client.post(
        "/api/v1/collection-tasks",
        json=direct_task_payload(auto_deliver=False),
    )

    assert response.status_code == 201
    assert response.json()["auto_deliver"] is False


def test_direct_task_rejects_unconfigured_auto_delivery(
    collection_client: TestClient,
) -> None:
    response = collection_client.post(
        "/api/v1/collection-tasks",
        json=direct_task_payload(auto_deliver=True),
    )

    assert response.status_code == 422


def test_direct_task_saves_explicit_auto_delivery(
    collection_client: TestClient,
) -> None:
    collection_client.app.state.erp_delivery.target_url = "https://receiver.example.test/erp"

    response = collection_client.post(
        "/api/v1/collection-tasks",
        json=direct_task_payload(auto_deliver=True),
    )

    assert response.status_code == 201
    assert response.json()["auto_deliver"] is True


def test_auto_delivery_is_off_by_default(collection_client: TestClient) -> None:
    task = create_natural_language_task(collection_client, "找武汉软装公司")

    assert task["auto_deliver"] is False


def test_delivery_config_exposes_only_configuration_state(
    collection_client: TestClient,
) -> None:
    response = collection_client.get("/api/v1/delivery-config")

    assert response.status_code == 200
    assert response.json() == {"configured": False}


def test_auto_delivery_requires_configured_erp(collection_client: TestClient) -> None:
    response = collection_client.post(
        "/api/v1/collection-tasks/from-natural-language",
        json={
            "request_text": "找武汉软装公司",
            "models": ["deepseek"],
            "auto_deliver": True,
        },
    )

    assert response.status_code == 422


def test_auto_delivery_can_be_enabled_when_erp_is_configured(
    collection_client: TestClient,
) -> None:
    collection_client.app.state.erp_delivery.target_url = "https://receiver.example.test/erp"

    response = collection_client.post(
        "/api/v1/collection-tasks/from-natural-language",
        json={
            "request_text": "找武汉软装公司",
            "models": ["deepseek"],
            "auto_deliver": True,
        },
    )

    assert response.status_code == 201
    assert response.json()["auto_deliver"] is True
    assert collection_client.get("/api/v1/delivery-config").json() == {"configured": True}


def test_retry_delivery_updates_lead_detail_and_export(
    collection_client: TestClient,
) -> None:
    erp_delivery = collection_client.app.state.erp_delivery
    erp_delivery.target_url = "https://receiver.example.test/erp"
    responses = iter(
        [
            httpx.Response(503),
            httpx.Response(200, json={"errCode": 0, "errMsg": None}),
        ]
    )
    erp_delivery._client = httpx.Client(
        transport=httpx.MockTransport(lambda request: next(responses))
    )
    task = create_natural_language_task(collection_client, "找武汉软装公司")
    lead = {
        "city": "武汉市",
        "company_name": "武汉失败重试公司",
        "normalized_company_name": "武汉失败重试公司",
        "mobile": "13800138000",
        "email": None,
        "wechat": None,
        "address": "武汉市测试路 1 号",
        "service_intro": "软装服务",
        "source_url": "https://example.test/retry",
        "discovery_channel": "doubao",
        "saved_at": "2026-07-28T10:00:00+08:00",
        "delivery_status": "failed",
        "delivery_error": "Service Unavailable",
        "delivered_at": None,
    }
    failed = erp_delivery.deliver(task, lead)
    assert failed.status == "failed"
    service = collection_client.app.state.collection_service
    service.store.update(
        task["id"],
        lambda stored: stored.update(
            status="completed",
            leads=[lead],
            lead_count=1,
        ),
    )

    retry = collection_client.post(
        f"/api/v1/collection-tasks/{task['id']}/retry-delivery"
    )

    assert retry.status_code == 200
    assert retry.json() == {"attempted": 1, "succeeded": 1, "failed": 0}
    persisted = collection_client.get(
        f"/api/v1/collection-tasks/{task['id']}/leads"
    ).json()["items"][0]
    assert persisted["delivery_status"] == "succeeded"
    assert persisted["delivery_error"] is None
    assert persisted["delivered_at"]

    exported = collection_client.get(
        f"/api/v1/collection-tasks/{task['id']}/export?format=json"
    )
    with ZipFile(BytesIO(exported.content)) as archive:
        row = __import__("json").loads(archive.read("武汉市.json"))[0]
    assert row["delivery_status"] == "succeeded"
    assert row["delivery_error"] == "未公开"
    assert row["delivered_at"] == persisted["delivered_at"]


def test_manual_delivery_does_not_wait_for_running_collection_lock(
    collection_client: TestClient,
) -> None:
    service = collection_client.app.state.collection_service
    task = create_natural_language_task(collection_client, "找武汉软装公司")
    lead = {
        "city": "武汉市",
        "company_name": "并行推送公司",
        "normalized_company_name": "并行推送公司",
        "mobile": "13800138000",
        "email": None,
        "wechat": None,
        "source_url": "https://example.test/parallel",
        "saved_at": "2026-07-28T10:00:00+08:00",
        "delivery_status": "display_only",
    }
    service.store.update(
        task["id"],
        lambda stored: stored.update(leads=[lead], lead_count=1),
    )

    request_started = Event()
    request_release = Event()

    def handler(_: httpx.Request) -> httpx.Response:
        request_started.set()
        assert request_release.wait(timeout=2)
        return httpx.Response(200, json={"errCode": 0, "errMsg": None})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    operation_lock = service._task_operation_lock(task["id"])
    operation_lock.acquire()
    stale_collection_snapshot = service.store.load(task["id"])
    result: dict[str, object] = {}

    def push() -> None:
        result.update(
            service.deliver_lead(
                task["id"],
                city="武汉市",
                company_name="并行推送公司",
                target_url="https://receiver.example.test/erp",
                client=client,
            )
        )

    thread = Thread(target=push)
    thread.start()
    try:
        assert request_started.wait(timeout=1), "manual delivery waited for collection lock"
        stale_collection_snapshot["leads"][0]["service_intro"] = "提供住宅软装设计服务"
        service._save_progress(stale_collection_snapshot)
        request_release.set()
        thread.join(timeout=2)
        assert not thread.is_alive()
    finally:
        request_release.set()
        if thread.is_alive():
            thread.join(timeout=2)
        operation_lock.release()
        client.close()

    assert result["status"] == "succeeded"
    persisted = service.store.load(task["id"])["leads"][0]
    assert persisted["delivery_status"] == "succeeded"
    assert persisted["service_intro"] == "提供住宅软装设计服务"


def test_task_list_and_control_endpoints(collection_client: TestClient) -> None:
    first = create_natural_language_task(collection_client, "找武汉软装公司")
    second = create_natural_language_task(collection_client, "找长沙整装公司")

    listed = collection_client.get("/api/v1/collection-tasks")

    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["items"]][:2] == [second["id"], first["id"]]

    for action in ["pause", "pause", "resume", "terminate", "terminate"]:
        response = collection_client.post(f"/api/v1/collection-tasks/{first['id']}/{action}")

        assert response.status_code == 202
        assert response.json()["id"] == first["id"]
        assert response.json()["status"]


@pytest.mark.parametrize("action", ["pause", "resume", "terminate"])
def test_control_of_unknown_task_returns_not_found(
    collection_client: TestClient,
    action: str,
) -> None:
    response = collection_client.post(f"/api/v1/collection-tasks/unknown/{action}")

    assert response.status_code == 404


@pytest.mark.parametrize("action", ["pause", "resume"])
def test_completed_task_rejects_pause_and_resume(
    collection_client: TestClient,
    action: str,
) -> None:
    task = create_natural_language_task(collection_client, "找武汉软装公司")
    service = collection_client.app.state.collection_service

    service.store.update(task["id"], lambda stored: stored.update(status="completed"))
    response = collection_client.post(f"/api/v1/collection-tasks/{task['id']}/{action}")

    assert response.status_code == 409


@pytest.mark.parametrize(
    ("action", "initial_status"),
    [("pause", "running"), ("resume", "paused")],
)
def test_control_rejects_task_that_completes_before_atomic_transition(
    collection_client: TestClient,
    monkeypatch,
    action: str,
    initial_status: str,
) -> None:
    task = create_natural_language_task(collection_client, "找武汉软装公司")
    service = collection_client.app.state.collection_service
    service.store.update(task["id"], lambda stored: stored.update(status=initial_status))
    original_update = service.store.update

    def complete_before_control(task_id: str, mutation):
        original_update(task_id, lambda stored: stored.update(status="completed"))
        return original_update(task_id, mutation)

    monkeypatch.setattr(service.store, "update", complete_before_control)
    response = collection_client.post(f"/api/v1/collection-tasks/{task['id']}/{action}")

    assert response.status_code == 409
