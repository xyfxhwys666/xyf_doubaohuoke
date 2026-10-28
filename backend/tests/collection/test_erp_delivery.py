from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock
from time import sleep

import httpx

from app.collection.erp_delivery import ErpLeadDelivery, build_erp_payload

ERP_URL = "https://receiver.example.test/erp"
TASK = {
    "id": "task-001",
    "name": "广州软装客户",
    "request_text": "查找广州软装公司，需要手机或邮箱",
}
LEAD = {
    "city": "广州市",
    "company_name": "广州梵花全屋软装有限公司",
    "normalized_company_name": "广州梵花全屋软装有限公司",
    "mobile": "13800138000",
    "email": "contact@example.com",
    "wechat": None,
    "address": "广州市天河区示例路 1 号",
    "service_intro": "提供全屋软装设计与落地服务",
    "source_url": "https://example.com/company",
    "discovery_channel": "doubao",
    "saved_at": "2026-07-25T12:33:23+08:00",
}


def test_build_erp_payload_uses_stable_guid_and_nested_contacts() -> None:
    first = build_erp_payload(TASK, LEAD)
    second = build_erp_payload(TASK, {**LEAD, "mobile": "13900000000"})
    web_chat = build_erp_payload(
        TASK,
        {**LEAD, "enrichment_model": "doubao_web_search"},
    )

    assert first["customerAccountGuid"] == second["customerAccountGuid"]
    assert first["sourceAccountName"] == "豆包"
    assert first["customerAccountName"] == LEAD["company_name"]
    assert first["customerCapturedAt"] == "2026-07-25 12:33:23"
    assert first["customerType"] == "B端客资"
    assert first["customerSourceChannel"] == "豆包"
    assert web_chat["customerSourceChannel"] == "豆包"
    assert first["sourceMethod"] == "AI联网采集"
    assert first["cEndCustomerRequirement"] == TASK["request_text"]
    assert first["contactInfo"]["contactInfoType"] == "手机号、邮箱"
    assert first["contactInfo"]["mobilePhone"] == LEAD["mobile"]
    assert first["contactInfo"]["email"] == LEAD["email"]
    assert "地址：" in first["contactInfo"]["otherContact"]
    assert "来源：" in first["contactInfo"]["otherContact"]


def test_business_error_is_queued_and_does_not_raise(tmp_path) -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"errCode": 49302, "errMsg": "IP not whitelisted"},
            )
        )
    )
    delivery = ErpLeadDelivery(tmp_path, ERP_URL, client=client)

    result = delivery.deliver(TASK, LEAD)

    assert result.status == "failed"
    assert result.error == "IP not whitelisted"
    assert delivery.failure_counts(TASK["id"]) == {"pending": 1}


def test_retry_removes_failure_after_erp_success(tmp_path) -> None:
    responses = iter(
        [
            httpx.Response(503),
            httpx.Response(
                200,
                json={"errCode": 0, "errMsg": None, "result": {}},
            ),
        ]
    )
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: next(responses))
    )
    delivery = ErpLeadDelivery(tmp_path, ERP_URL, client=client)

    first = delivery.deliver(TASK, LEAD)
    retried = delivery.retry(TASK["id"])

    assert first.status == "failed"
    assert retried == {"attempted": 1, "succeeded": 1, "failed": 0}
    assert delivery.failure_counts(TASK["id"]) == {"pending": 0}


def test_retry_preserves_same_company_failure_from_another_task(tmp_path) -> None:
    responses = iter(
        [
            httpx.Response(503),
            httpx.Response(503),
            httpx.Response(200, json={"errCode": 0, "errMsg": None}),
        ]
    )
    delivery = ErpLeadDelivery(
        tmp_path,
        ERP_URL,
        client=httpx.Client(transport=httpx.MockTransport(lambda request: next(responses))),
    )
    second_task = {**TASK, "id": "task-002", "request_text": "different requirement"}

    first = delivery.deliver(TASK, LEAD)
    second = delivery.deliver(second_task, LEAD)
    retried = delivery.retry_results(TASK["id"])

    assert first.status == second.status == "failed"
    assert len(retried) == 1
    assert retried[0].status == "completed"
    assert delivery.failure_counts(TASK["id"]) == {"pending": 0}
    assert delivery.failure_counts(second_task["id"]) == {"pending": 1}


def test_legacy_guid_failure_is_migrated_without_duplicate_delivery(tmp_path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"errCode": 0, "errMsg": None})

    delivery = ErpLeadDelivery(
        tmp_path,
        ERP_URL,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    payload = build_erp_payload(TASK, LEAD)
    fingerprint = delivery.fingerprint(TASK, LEAD)
    delivery._write(
        delivery.failures_path,
        {
            payload["customerAccountGuid"]: {
                "task_id": TASK["id"],
                "payload": payload,
                "fingerprint": fingerprint,
                "attempts": 1,
                "last_error": "temporary error",
            }
        },
    )

    pending = delivery.deliver(TASK, LEAD)
    retried = delivery.retry_results(TASK["id"])

    assert pending.status == "failed"
    assert [result.status for result in retried] == ["completed"]
    assert len(requests) == 1
    assert delivery.failure_counts(TASK["id"]) == {"pending": 0}


def test_unchanged_successful_lead_is_not_sent_twice(tmp_path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"errCode": 0, "errMsg": None})

    delivery = ErpLeadDelivery(
        tmp_path,
        ERP_URL,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    first = delivery.deliver(TASK, LEAD)
    second = delivery.deliver(TASK, LEAD)

    assert first.status == "completed"
    assert second.status == "skipped"
    assert len(requests) == 1


def test_concurrent_same_guid_is_posted_once_and_has_one_final_record(tmp_path) -> None:
    requests: list[httpx.Request] = []
    request_lock = Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        with request_lock:
            requests.append(request)
            request_number = len(requests)
        if request_number == 1:
            sleep(0.02)
            return httpx.Response(200, json={"errCode": 0, "errMsg": None})
        sleep(0.08)
        return httpx.Response(
            200,
            json={"errCode": 49302, "errMsg": "duplicate race"},
        )

    delivery = ErpLeadDelivery(
        tmp_path,
        ERP_URL,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    start = Barrier(2)

    def deliver() -> object:
        start.wait(timeout=2)
        return delivery.deliver(TASK, LEAD)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: deliver(), range(2)))

    assert sorted(result.status for result in results) == ["completed", "skipped"]
    assert len(requests) == 1
    assert delivery.failure_counts(TASK["id"]) == {"pending": 0}


def test_same_mobile_is_not_sent_again_when_task_payload_changes(tmp_path) -> None:
    requests: list[httpx.Request] = []
    delivery = ErpLeadDelivery(
        tmp_path,
        ERP_URL,
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: requests.append(request)
                or httpx.Response(200, json={"errCode": 0, "errMsg": None})
            )
        ),
    )

    first = delivery.deliver(TASK, {**LEAD, "contact_evidence_score": 85})
    second = delivery.deliver(
        {**TASK, "id": "task-002", "request_text": "另一个采集任务"},
        {**LEAD, "service_intro": "更新后的介绍", "contact_evidence_score": 85},
    )

    assert first.status == "completed"
    assert second.status == "skipped"
    assert len(requests) == 1


def test_stronger_new_mobile_updates_same_erp_customer(tmp_path) -> None:
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(__import__("json").loads(request.content))
        return httpx.Response(200, json={"errCode": 0, "errMsg": None})

    delivery = ErpLeadDelivery(
        tmp_path,
        ERP_URL,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    first = delivery.deliver(TASK, {**LEAD, "contact_evidence_score": 80})
    updated = delivery.deliver(
        {**TASK, "id": "task-002"},
        {**LEAD, "mobile": "13900139000", "contact_evidence_score": 95},
    )

    assert first.status == updated.status == "completed"
    assert len(payloads) == 2
    assert payloads[0]["customerAccountGuid"] == payloads[1]["customerAccountGuid"]
