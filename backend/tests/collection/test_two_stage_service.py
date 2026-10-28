from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Condition, Event, Lock, Thread
from time import monotonic, sleep
from typing import Any

import httpx
import pytest

from app.collection.doubao_attention import DoubaoAttentionMonitor
from app.collection.doubao_browser import (
    DoubaoBrowserStatus,
    DoubaoChallengeRequired,
    DoubaoLoginRequired,
    DoubaoWebTimeout,
    DoubaoWebUnavailable,
)
from app.collection.doubao_web import (
    DoubaoWebCandidate,
    DoubaoWebContact,
    DoubaoWebResponseError,
)
from app.collection.erp_delivery import ErpLeadDelivery
from app.collection.models import ModelVerificationError
from app.collection.normalization import normalize_lead
from app.collection.service import CollectionService, InvalidTaskStateError, TaskStore
from app.collection.web_search import CompanyNameEvidence, ContactEvidence


class RecordingStore(TaskStore):
    def __init__(self, data_dir: Path) -> None:
        super().__init__(data_dir)
        self.snapshots: list[dict[str, Any]] = []
        self.lead_batches: list[list[dict[str, Any]]] = []
        self.reconciliations: list[tuple[str, list[dict[str, Any]]]] = []

    def save(self, task: dict[str, Any]) -> None:
        self.snapshots.append(deepcopy(task))
        super().save(task)

    def save_leads(self, task_id: str, leads: list[dict[str, Any]]) -> None:
        batch = deepcopy(leads)
        self.lead_batches.append(batch)
        super().save_leads(task_id, batch)

    def reconcile_leads(
        self,
        task_id: str,
        city: str,
        leads: list[dict[str, Any]],
    ) -> None:
        batch = deepcopy(leads)
        self.reconciliations.append((city, batch))
        super().reconcile_leads(task_id, city, batch)


class EmptyProvider:
    id = "web_search"
    label = "公开网页"
    configured = True

    def collect(self, *, city: str, keyword: str, request_text: str) -> list[dict[str, Any]]:
        return []


class RecordingCompanyNameVerifier:
    def __init__(self, statuses: dict[str, str]) -> None:
        self.statuses = statuses
        self.calls: Counter[str] = Counter()
        self.lock = Lock()
        self.active = 0
        self.max_active = 0
        self.source_urls: dict[str, list[str]] = {}

    def verify_company_name(
        self,
        *,
        company_name: str,
        city: str,
        keywords: list[str],
        source_urls: list[str] | None = None,
    ) -> CompanyNameEvidence:
        del city, keywords
        with self.lock:
            self.calls[company_name] += 1
            self.source_urls[company_name] = list(source_urls or [])
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            sleep(0.02)
            status = self.statuses.get(company_name, "unverified")
            return CompanyNameEvidence(
                status=status,
                evidence_urls=[f"https://evidence.example/{company_name[-1]}"],
                reason=f"evidence: {status}",
                city_matched=status == "verified_public_web",
                keyword_matched=status == "verified_public_web",
            )
        finally:
            with self.lock:
                self.active -= 1


class RecordingContactVerifier:
    def __init__(self, statuses: dict[str, str]) -> None:
        self.statuses = statuses
        self.calls: Counter[str] = Counter()

    def verify_contact_evidence(
        self,
        *,
        company_name: str,
        mobile: str | None,
        email: str | None = None,
        source_url: str | None,
        source_urls: list[str] | None = None,
        contact_person_name: str | None,
        contact_person_role: str | None,
    ) -> ContactEvidence:
        del mobile, email, source_urls, contact_person_name, contact_person_role
        self.calls[company_name] += 1
        status = self.statuses.get(company_name, "rejected")
        if status not in {
            "verified_public_web",
            "verified_official_page",
            "verified_cross_source",
        }:
            return ContactEvidence(
                status=status,
                evidence_url=source_url,
                evidence_quote="",
                reason=f"contact evidence: {status}",
            )
        return ContactEvidence(
            status=status,
            evidence_url=source_url,
            evidence_quote=f"{company_name} 业务负责人张三 13800138001",
            reason="contact evidence: verified",
            contact_person_name="张三",
            contact_person_role="业务负责人",
        )


class LegacyProvider(EmptyProvider):
    def collect(self, *, city: str, keyword: str, request_text: str) -> list[dict[str, Any]]:
        mobile = "13800138000" if keyword == "软装公司" else "13800138001"
        return [
            {
                "company_name": f"{city}{keyword}服务中心",
                "mobile": mobile,
                "source_url": "https://example.test/legacy",
            }
        ]


class TwoStageVerifier:
    id = "deepseek"
    label = "DeepSeek"
    configured = True

    def __init__(self) -> None:
        self.discovery_calls: list[tuple[str, list[str]]] = []
        self.enrichment_calls: list[str] = []
        self.lock = Lock()
        self.active = 0
        self.max_active = 0

    def discover_companies(
        self,
        *,
        request_text: str,
        city: str,
        keywords: list[str],
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        self.discovery_calls.append((city, keywords))
        return [
            {
                "company_name": f"{city}真实装饰公司{i}",
                "matched_keyword": keywords[i % len(keywords)],
                "reason": "符合客户类型",
                "source_confidence": 70 + i,
            }
            for i in range(4)
        ]

    def enrich_company(
        self,
        *,
        request_text: str,
        city: str,
        company: dict[str, Any],
        required_contacts: list[str],
    ) -> dict[str, Any]:
        name = company["company_name"]
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            self.enrichment_calls.append(name)
            sleep(0.02)
            suffix = int(name[-1])
            return {
                "company_name": name,
                "mobile": "027-88886666" if suffix == 0 else f"1380013800{suffix}",
                "email": None,
                "wechat": None,
                "address": f"{city}公开地址",
                "service_intro": "提供全案装饰服务",
                "source_url": f"https://example.test/{suffix}",
                "matched_keyword": company["matched_keyword"],
                "source_confidence": 78,
            }
        finally:
            with self.lock:
                self.active -= 1


class FailingEnricher(TwoStageVerifier):
    id = "doubao"
    label = "豆包"

    def enrich_company(self, **kwargs: Any) -> dict[str, Any]:
        raise ModelVerificationError("temporary enrichment failure")


class VerifyingTwoStageVerifier(TwoStageVerifier):
    def verify(
        self,
        leads: list[dict[str, Any]],
        *,
        request_text: str,
        city: str,
        keyword: str,
    ) -> list[dict[str, Any]]:
        return [{"relevant": True, "reason": request_text} for _ in leads]


class DuplicateContactEnricher(TwoStageVerifier):
    id = "doubao"
    label = "豆包"

    def enrich_company(self, **kwargs: Any) -> dict[str, Any]:
        enriched = super().enrich_company(**kwargs)
        enriched["mobile"] = (
            "13800138000" if kwargs["city"] == "武汉市" else "13800138001"
        )
        return enriched


class BlockingInFlightEnricher:
    id = "doubao"
    label = "豆包"
    configured = True

    def __init__(self) -> None:
        self._condition = Condition()
        self.release = Event()
        self.call_counts: Counter[str] = Counter()

    def enrich_company(
        self,
        *,
        request_text: str,
        city: str,
        company: dict[str, Any],
        required_contacts: list[str],
    ) -> dict[str, Any]:
        name = company["company_name"]
        with self._condition:
            self.call_counts[name] += 1
            self._condition.notify_all()
        self.release.wait(timeout=3)
        return {
            **company,
            "mobile": f"1380013800{int(name[-1])}",
            "source_url": "https://example.test/in-flight",
        }

    def wait_for_calls(self, count: int, *, timeout: float = 2.0) -> bool:
        with self._condition:
            return self._condition.wait_for(
                lambda: sum(self.call_counts.values()) >= count,
                timeout=timeout,
            )


class LegacyVerifier:
    id = "deepseek"
    label = "DeepSeek"
    configured = True

    def verify(
        self,
        leads: list[dict[str, Any]],
        *,
        request_text: str,
        city: str,
        keyword: str,
    ) -> list[dict[str, Any]]:
        return [{"relevant": True, "reason": request_text} for _ in leads]


class RecordingApiVerifier(LegacyVerifier):
    id = "doubao"
    label = "豆包"

    def __init__(self) -> None:
        self.calls = 0

    def verify(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls += 1
        return super().verify(*args, **kwargs)


class BlockingLegacyVerifier(LegacyVerifier):
    def __init__(self) -> None:
        self.started = Event()
        self.release = Event()

    def verify(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        self.started.set()
        self.release.wait(timeout=3)
        return super().verify(*args, **kwargs)


class RecordingDelivery:
    def __init__(self, store: TaskStore) -> None:
        self.store = store
        self.delivered: list[dict[str, Any]] = []

    def deliver(self, task: dict[str, Any], lead: dict[str, Any]) -> Any:
        persisted = self.store.load(task["id"])["leads"]
        assert any(
            item["company_name"] == lead["company_name"]
            and (item.get("mobile") or item.get("email") or item.get("wechat"))
            for item in persisted
        )
        self.delivered.append(deepcopy(lead))
        return type("Result", (), {"status": "completed", "error": None})()


class BlockingRecordingDelivery(RecordingDelivery):
    def __init__(self, store: TaskStore, *, status: str = "completed") -> None:
        super().__init__(store)
        self.status = status
        self.sending = Event()
        self.release = Event()

    def deliver(self, task: dict[str, Any], lead: dict[str, Any]) -> Any:
        persisted = next(
            item
            for item in self.store.list_leads(task["id"])
            if item["company_name"] == lead["company_name"]
        )
        assert persisted["delivery_status"] == "sending"
        self.delivered.append(deepcopy(lead))
        self.sending.set()
        self.release.wait(timeout=3)
        error = "ERP rejected lead" if self.status == "failed" else None
        return type("Result", (), {"status": self.status, "error": error})()

    def wait_until_sending(self, *, timeout: float = 2.0) -> bool:
        return self.sending.wait(timeout=timeout)


class FixedResultDelivery(RecordingDelivery):
    def __init__(self, store: TaskStore, *, status: str, error: str | None = None) -> None:
        super().__init__(store)
        self.status = status
        self.error = error

    def deliver(self, task: dict[str, Any], lead: dict[str, Any]) -> Any:
        self.delivered.append(deepcopy(lead))
        return type("Result", (), {"status": self.status, "error": self.error})()


class PendingBlockingStore(RecordingStore):
    def __init__(self, data_dir: Path) -> None:
        super().__init__(data_dir)
        self.pending = Event()
        self.release_pending = Event()
        self._blocked = False

    def save_leads(self, task_id: str, leads: list[dict[str, Any]]) -> None:
        super().save_leads(task_id, leads)
        if not self._blocked and any(
            lead.get("delivery_status") == "pending" for lead in leads
        ):
            self._blocked = True
            self.pending.set()
            self.release_pending.wait(timeout=3)


class PauseBeforeFirstEnrichmentService(CollectionService):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.paused_before_enrichment = False

    def _enrich_city_candidates(self, task: dict[str, Any], city: str) -> None:
        if not self.paused_before_enrichment:
            self.paused_before_enrichment = True
            self.pause_task(task["id"])
        super()._enrich_city_candidates(task, city)


class FakeDoubaoWebGateway:
    def __init__(
        self,
        *,
        interrupt_company: str | None = None,
        discovery_batches: list[list[int]] | None = None,
        interrupt_error: type[Exception] = DoubaoLoginRequired,
    ) -> None:
        self.interrupt_company = interrupt_company
        self.interrupt_error = interrupt_error
        self.ready = True
        self.discovery_calls: list[str] = []
        self.discovery_requests: list[str] = []
        self.enrichment_calls: list[str] = []
        self.interrupted = False
        self.status_failure = False
        self.discovery_batches = discovery_batches or [[0, 1]]
        self.pause_after_discovery: Callable[[], None] | None = None
        self.public_search_results: list[dict[str, str]] = []
        self.public_search_calls: list[str] = []

    def status(self) -> DoubaoBrowserStatus:
        if self.status_failure:
            raise DoubaoWebUnavailable("豆包浏览器不可用")
        state = "ready" if self.ready else "waiting_for_login"
        return DoubaoBrowserStatus(state, state, None)

    def refresh_status(self) -> DoubaoBrowserStatus:
        return self.status()

    def discover_companies(
        self, *, city: str, keywords: list[str], request_text: str
    ) -> list[DoubaoWebCandidate]:
        self.discovery_calls.append(city)
        self.discovery_requests.append(request_text)
        batch_index = min(len(self.discovery_calls) - 1, len(self.discovery_batches) - 1)
        result = [
            DoubaoWebCandidate(
                company_name=f"{city}网页设计公司{i}",
                city=city,
                matched_keyword=keywords[i % len(keywords)],
                source_urls=[f"https://example.test/company-{i}"],
                evidence_quotes=[f"公司{i}主营别墅全案"],
                raw_response="raw",
            )
            for i in self.discovery_batches[batch_index]
        ]
        if self.pause_after_discovery is not None:
            callback, self.pause_after_discovery = self.pause_after_discovery, None
            callback()
        return result

    def enrich_company(
        self,
        *,
        company_name: str,
        city: str,
        requested_contacts: list[str],
    ) -> DoubaoWebContact:
        del requested_contacts
        self.enrichment_calls.append(company_name)
        if company_name == self.interrupt_company and not self.interrupted:
            self.interrupted = True
            self.ready = False
            raise self.interrupt_error("需要人工处理豆包网页")
        suffix = company_name[-1]
        return DoubaoWebContact(
            company_name=company_name,
            mobile=f"1380013800{suffix}",
            contact_person_name=None,
            contact_person_role=None,
            email=None,
            wechat=None,
            address=f"{city}公开地址",
            service_intro="别墅全案设计",
            source_urls=[f"https://example.test/contact-{suffix}"],
            evidence_quotes=[f"{company_name} 1380013800{suffix}"],
            raw_response="raw",
        )

    def search_public_web(
        self, *, query: str, max_results: int = 10
    ) -> list[dict[str, str]]:
        del max_results
        self.public_search_calls.append(query)
        return list(self.public_search_results)

    def shutdown(self) -> None:
        return None


class TimeoutThenRecoverGateway(FakeDoubaoWebGateway):
    def __init__(self) -> None:
        super().__init__()
        self.reconnect_calls = 0
        self.timeout_once = True

    def reconnect(self) -> DoubaoBrowserStatus:
        self.reconnect_calls += 1
        self.ready = True
        return DoubaoBrowserStatus("ready", "豆包网页已重新连接", None)

    def enrich_company(self, **kwargs: Any) -> DoubaoWebContact:
        if self.timeout_once:
            self.timeout_once = False
            raise DoubaoWebTimeout("等待豆包网页回答超时")
        return super().enrich_company(**kwargs)


class AlwaysTimeoutGateway(FakeDoubaoWebGateway):
    def enrich_company(self, **kwargs: Any) -> DoubaoWebContact:
        self.enrichment_calls.append(str(kwargs["company_name"]))
        raise DoubaoWebTimeout("等待豆包网页回答超时")


class BlockingDoubaoWebGateway(FakeDoubaoWebGateway):
    def __init__(self) -> None:
        super().__init__(discovery_batches=[[0]])
        self.first_call_started = Event()
        self.release_first_call = Event()
        self.active_calls = 0
        self.max_active_calls = 0
        self._calls_lock = Lock()

    def discover_companies(
        self, *, city: str, keywords: list[str], request_text: str
    ) -> list[DoubaoWebCandidate]:
        with self._calls_lock:
            self.active_calls += 1
            self.max_active_calls = max(self.max_active_calls, self.active_calls)
            first_call = len(self.discovery_calls) == 0
        try:
            if first_call:
                self.first_call_started.set()
                self.release_first_call.wait(timeout=2)
            return super().discover_companies(
                city=city,
                keywords=keywords,
                request_text=request_text,
            )
        finally:
            with self._calls_lock:
                self.active_calls -= 1


def _payload(models: list[str]) -> dict[str, Any]:
    return {
        "name": "两阶段采集",
        "request_text": "找武汉软装和整装公司",
        "cities": ["武汉市"],
        "all_cities": False,
        "keywords": ["软装公司", "整装公司"],
        "providers": ["web_search"],
        "models": models,
        "required_contacts": ["mobile"],
        "contact_match": "any",
        "limit_per_city": None,
    }


def test_doubao_web_tasks_share_one_account_lease(tmp_path: Path) -> None:
    gateway = BlockingDoubaoWebGateway()
    service = CollectionService(
        TaskStore(tmp_path),
        {"web_search": EmptyProvider()},
        {"deepseek": TwoStageVerifier()},
        doubao_web_gateway=gateway,
        max_concurrent_tasks=2,
    )
    payload = {
        **_payload(["deepseek"]),
        "use_doubao_web_chat": True,
        "web_chat_discovery": True,
        "limit_per_city": 1,
    }
    first = service.create_task(payload)
    second = service.create_task(payload)
    service.prepare_start(first["id"])
    service.prepare_start(second["id"])
    first_thread = Thread(target=service.run_task, args=(first["id"],))
    second_thread = Thread(target=service.run_task, args=(second["id"],))
    try:
        first_thread.start()
        assert gateway.first_call_started.wait(timeout=1)
        second_thread.start()
        sleep(0.05)

        assert gateway.max_active_calls == 1
        assert len(gateway.discovery_calls) == 0

        gateway.release_first_call.set()
        first_thread.join(timeout=3)
        second_thread.join(timeout=3)

        assert not first_thread.is_alive()
        assert not second_thread.is_alive()
        assert gateway.max_active_calls == 1
    finally:
        gateway.release_first_call.set()
        service.shutdown()


def test_candidate_persistence_excludes_erp_delivered_company(tmp_path: Path) -> None:
    delivery = ErpLeadDelivery(tmp_path, target_url="https://erp.example.test/leads")
    delivered = delivery.dedup_ledger.evaluate(
        city="深圳市",
        company_name="深圳市历史推送设计有限公司",
        mobile="13800138002",
        evidence_score=90,
    )
    delivery.dedup_ledger.mark_delivered(delivered)
    service = CollectionService(
        TaskStore(tmp_path),
        {"web_search": EmptyProvider()},
        {},
        lead_delivery=delivery,
    )
    task = service.create_task(_payload([]))
    try:
        service._persist_discovered_candidates(
            task,
            "深圳市",
            [
                {
                    "company_name": "深圳市历史推送设计有限公司",
                    "city": "深圳市",
                    "matched_keyword": "软装公司",
                    "discovery_channel": "doubao_web",
                }
            ],
            10,
        )

        assert service.get_task(task["id"])["leads"] == []
    finally:
        service.shutdown()


def test_service_backfills_successful_history_into_erp_dedup_ledger(
    tmp_path: Path,
) -> None:
    delivery = ErpLeadDelivery(tmp_path, target_url=None)
    store = TaskStore(tmp_path)
    service = CollectionService(store, {"web_search": EmptyProvider()}, lead_delivery=delivery)
    task = service.create_task(_payload(["doubao"]))
    task["leads"] = [
        normalize_lead(
            {
                "company_name": "深圳市历史成功推送设计有限公司",
                "mobile": "13800138009",
                "email": "history@example.test",
            },
            city="深圳市",
            provider="doubao",
        )
    ]
    task["leads"][0]["delivery_status"] = "succeeded"
    store.save(task)
    service.shutdown()

    restarted = CollectionService(
        store,
        {"web_search": EmptyProvider()},
        lead_delivery=delivery,
    )
    try:
        assert delivery.is_company_delivered(
            "深圳市", "深圳市历史成功推送设计有限公司"
        )
    finally:
        restarted.shutdown()


def test_candidate_persistence_fills_limit_after_excluding_erp_delivered_company(
    tmp_path: Path,
) -> None:
    delivery = ErpLeadDelivery(tmp_path, target_url="https://erp.example.test/leads")
    delivered = delivery.dedup_ledger.evaluate(
        city="深圳市",
        company_name="深圳市历史推送设计有限公司",
        mobile="13800138003",
        evidence_score=90,
    )
    delivery.dedup_ledger.mark_delivered(delivered)
    service = CollectionService(
        TaskStore(tmp_path),
        {"web_search": EmptyProvider()},
        {},
        lead_delivery=delivery,
    )
    task = service.create_task(_payload(["doubao"]))
    try:
        service._persist_discovered_candidates(
            task,
            "深圳市",
            [
                {
                    "company_name": "深圳市历史推送设计有限公司",
                    "city": "深圳市",
                    "matched_keyword": "软装公司",
                },
                {
                    "company_name": "深圳市新发现设计有限公司",
                    "city": "深圳市",
                    "matched_keyword": "软装公司",
                },
            ],
            1,
        )

        assert [lead["company_name"] for lead in service.get_task(task["id"])["leads"]] == [
            "深圳市新发现设计有限公司"
        ]
    finally:
        service.shutdown()


def test_candidate_persistence_matches_existing_company_after_name_normalization(
    tmp_path: Path,
) -> None:
    service = CollectionService(TaskStore(tmp_path), {"web_search": EmptyProvider()})
    task = service.create_task(_payload(["doubao"]))
    try:
        service._persist_discovered_candidates(
            task,
            "深圳市",
            [
                {
                    "company_name": "深圳市规范 设计有限公司",
                    "city": "深圳市",
                    "matched_keyword": "软装公司",
                }
            ],
            10,
        )
        service._persist_discovered_candidates(
            task,
            "深圳市",
            [
                {
                    "company_name": "深圳市规范设计有限公司",
                    "city": "深圳市",
                    "matched_keyword": "全案设计公司",
                }
            ],
            10,
        )

        leads = service.get_task(task["id"])["leads"]
        assert [lead["company_name"] for lead in leads] == ["深圳市规范设计有限公司"]
    finally:
        service.shutdown()


def test_candidate_persistence_merges_contact_fields_into_existing_company(
    tmp_path: Path,
) -> None:
    service = CollectionService(TaskStore(tmp_path), {"web_search": EmptyProvider()})
    task = service.create_task(_payload(["doubao"]))
    try:
        service._persist_discovered_candidates(
            task,
            "Shenzhen",
            [{"company_name": "Shenzhen Rich Design Co", "city": "Shenzhen"}],
            10,
        )
        service._persist_discovered_candidates(
            task,
            "Shenzhen",
            [
                {
                    "company_name": " Shenzhen Rich Design Co ",
                    "city": "Shenzhen",
                    "mobile": "13800138000",
                    "email": "sales@example.test",
                    "source_url": "https://example.test/contact",
                }
            ],
            10,
        )

        leads = service.get_task(task["id"])["leads"]
        assert len(leads) == 1
        assert leads[0]["mobile"] == "13800138000"
        assert leads[0]["email"] == "sales@example.test"
        assert leads[0]["source_url"] == "https://example.test/contact"
    finally:
        service.shutdown()


def test_candidate_persistence_deduplicates_preexisting_company_variants(
    tmp_path: Path,
) -> None:
    service = CollectionService(TaskStore(tmp_path), {"web_search": EmptyProvider()})
    task = service.create_task(_payload(["doubao"]))
    task["leads"] = [
        normalize_lead(
            {"company_name": "深圳市采集 重复设计有限公司"},
            city="深圳市",
            provider="doubao_web",
        ),
        normalize_lead(
            {
                "company_name": "深圳市采集重复设计有限公司",
                "mobile": "13800138000",
                "email": "sales@example.test",
            },
            city="深圳市",
            provider="doubao_web",
        ),
    ]
    service.store.save(task)

    try:
        service._persist_discovered_candidates(
            task,
            "深圳市",
            [{"company_name": "深圳市采集重复设计有限公司", "city": "深圳市"}],
            10,
        )

        leads = service.get_task(task["id"])["leads"]
        assert len(leads) == 1
        assert leads[0]["company_name"] == "深圳市采集重复设计有限公司"
        assert leads[0]["mobile"] == "13800138000"
        assert leads[0]["email"] == "sales@example.test"
    finally:
        service.shutdown()


def test_web_chat_defaults_and_multisource_evidence_normalization(tmp_path: Path) -> None:
    service = CollectionService(TaskStore(tmp_path), {"web_search": EmptyProvider()})
    task = service.create_task(_payload([]))
    try:
        assert task["use_doubao_web_chat"] is False
        assert task["web_chat_discovery"] is False
        assert task["web_chat_status"] == "disabled"
        assert task["web_chat_error"] is None
        assert task["web_chat_current_company"] is None
        assert task["web_chat_checkpoint"] == {}
        assert CollectionService.task_summary(task)["web_chat_status"] == "disabled"

        lead = normalize_lead(
            {
                "company_name": "武汉多源证据公司",
                "contact_evidence_urls": ["https://example.test/contact"],
                "contact_evidence_quotes": ["官网公开了业务负责人电话"],
            },
            city="武汉市",
            provider="web_search",
        )
        assert lead["contact_evidence_urls"] == ["https://example.test/contact"]
        assert lead["contact_evidence_quotes"] == ["官网公开了业务负责人电话"]
        assert lead["contact_evidence_url"] == "https://example.test/contact"
        assert lead["contact_evidence_quote"] == "官网公开了业务负责人电话"
    finally:
        service.shutdown()


def test_new_tasks_do_not_share_mutable_web_chat_checkpoints(tmp_path: Path) -> None:
    service = CollectionService(TaskStore(tmp_path), {"web_search": EmptyProvider()})
    try:
        first = service.create_task(_payload([]))
        first["web_chat_checkpoint"].update(
            city="武汉市",
            batch_number=9,
            processed_normalized_names=["武汉甲公司"],
        )

        second = service.create_task(_payload([]))

        assert second["web_chat_checkpoint"] == {}
    finally:
        service.shutdown()


def test_pure_web_chat_verification_never_calls_model_api(tmp_path: Path) -> None:
    verifier = RecordingApiVerifier()
    service = CollectionService(
        TaskStore(tmp_path),
        {"web_search": EmptyProvider()},
        {"doubao": verifier},
    )
    task = service.create_task(
        {
            **_payload(["doubao"]),
            "use_doubao_web_chat": True,
            "web_chat_discovery": True,
        }
    )
    task["leads"] = [
        {
            "city": "武汉市",
            "company_name": "武汉市真实设计有限公司",
            "matched_keyword": "软装公司",
            "mobile": "13800138000",
            "company_name_verification_status": "verified_public_web",
            "contact_verification_status": "verified_official_page",
            "contact_evidence_url": "https://example.test/contact",
            "verification_status": "unverified",
            "verification_models": [],
            "verification_detail": {},
        }
    ]
    service.store.save(task)

    service.verify_task(task["id"])

    verified = service.get_task(task["id"])["leads"][0]
    assert verifier.calls == 0
    assert verified["verification_status"] == "cross_verified"
    assert verified["verification_models"] == ["doubao"]


def test_hybrid_mode_discovers_with_selected_model_and_enriches_through_web_chat(
    tmp_path: Path,
) -> None:
    gateway = FakeDoubaoWebGateway()
    deepseek = TwoStageVerifier()
    payload = {**_payload(["deepseek"]), "use_doubao_web_chat": True, "limit_per_city": 2}
    service = CollectionService(
        TaskStore(tmp_path),
        {"web_search": EmptyProvider()},
        {"deepseek": deepseek},
        doubao_web_gateway=gateway,
    )
    task = service.create_task(payload)
    try:
        service.prepare_start(task["id"])
        service.run_task(task["id"])
        completed = service.get_task(task["id"])

        assert deepseek.discovery_calls == [("武汉市", ["软装公司", "整装公司"])]
        assert deepseek.enrichment_calls == []
        assert gateway.discovery_calls == []
        assert gateway.enrichment_calls == [
            "武汉市真实装饰公司0",
            "武汉市真实装饰公司1",
        ]
        assert completed["status"] == "completed"
        assert completed["models"] == ["deepseek"]
        assert completed["web_chat_checkpoint"]["processed_normalized_names"] == [
            "武汉市真实装饰公司0",
            "武汉市真实装饰公司1",
        ]
    finally:
        service.shutdown()


def test_web_chat_enriches_source_backed_unverified_company_candidates(
    tmp_path: Path,
) -> None:
    class FixedContactGateway(FakeDoubaoWebGateway):
        def enrich_company(
            self,
            *,
            company_name: str,
            city: str,
            requested_contacts: list[str],
        ) -> DoubaoWebContact:
            del requested_contacts
            self.enrichment_calls.append(company_name)
            return DoubaoWebContact(
                company_name=company_name,
                mobile="13800138009",
                contact_person_name=None,
                contact_person_role=None,
                email=None,
                wechat=None,
                address=f"{city}公开地址",
                service_intro="别墅全案设计",
                source_urls=["https://evidence.example/contact"],
                evidence_quotes=[f"{company_name} 13800138009"],
                raw_response="raw",
            )

    company_name = "武汉市待补全设计有限公司"
    gateway = FixedContactGateway()
    gateway.public_search_results = [
        {
            "title": company_name,
            "text": f"{company_name} 联系电话 13800138009 武汉市",
            "url": "https://evidence.example/contact",
        }
    ]
    company_verifier = RecordingCompanyNameVerifier({company_name: "unverified"})
    service = CollectionService(
        TaskStore(tmp_path),
        {"web_search": EmptyProvider()},
        company_name_verifier=company_verifier,
        doubao_web_gateway=gateway,
    )
    task = service.create_task(
        {
            **_payload([]),
            "use_doubao_web_chat": True,
            "web_chat_discovery": True,
        }
    )
    task["leads"] = [
        {
            "city": "武汉市",
            "company_name": company_name,
            "source_url": "https://evidence.example/company",
            "company_name_evidence_urls": ["https://evidence.example/company"],
            "candidate_reason": "武汉市公开页面显示其提供别墅全案设计",
            "company_name_verification_status": "unverified",
            "enrichment_state": "pending",
        }
    ]
    service.store.save(task)

    try:
        service._enrich_city_candidates_web(task, "武汉市")
        enriched = service.get_task(task["id"])["leads"][0]
    finally:
        service.shutdown()

    assert gateway.enrichment_calls == [company_name]
    assert enriched["mobile"] == "13800138009"
    assert enriched["contact_verification_status"] == "verified_public_web"
    assert enriched["company_name_verification_status"] == "verified_public_web"


def test_web_chat_enrichment_does_not_clear_existing_candidate_fields(
    tmp_path: Path,
) -> None:
    class SparseContactGateway(FakeDoubaoWebGateway):
        def enrich_company(
            self,
            *,
            company_name: str,
            city: str,
            requested_contacts: list[str],
        ) -> DoubaoWebContact:
            del requested_contacts
            self.enrichment_calls.append(company_name)
            return DoubaoWebContact(
                company_name=company_name,
                mobile=None,
                contact_person_name=None,
                contact_person_role=None,
                email=None,
                wechat=None,
                address=None,
                service_intro=None,
                source_urls=[],
                evidence_quotes=[],
                raw_response="没有找到更多公开字段",
            )

    company_name = "武汉市已有字段设计公司"
    gateway = SparseContactGateway()
    service = CollectionService(
        TaskStore(tmp_path),
        {"web_search": EmptyProvider()},
        doubao_web_gateway=gateway,
    )
    task = service.create_task(
        {
            **_payload([]),
            "use_doubao_web_chat": True,
            "web_chat_discovery": True,
        }
    )
    task["leads"] = [
        {
            "city": "武汉市",
            "company_name": company_name,
            "mobile": "13800138000",
            "address": "武汉市已有地址",
            "service_intro": "已有别墅全案设计介绍",
            "source_url": "https://evidence.example/existing",
            "company_name_evidence_urls": ["https://evidence.example/existing"],
            "company_name_verification_status": "unverified",
            "contact_verification_status": "unverified",
            "enrichment_state": "pending",
        }
    ]
    service.store.save(task)

    try:
        service._enrich_city_candidates_web(task, "武汉市")
        enriched = service.get_task(task["id"])["leads"][0]
    finally:
        service.shutdown()

    assert enriched["mobile"] == "13800138000"
    assert enriched["address"] == "武汉市已有地址"
    assert enriched["service_intro"] == "已有别墅全案设计介绍"
    assert enriched["source_url"] == "https://evidence.example/existing"


def test_web_chat_does_not_wait_for_separate_company_search_gate(
    tmp_path: Path,
) -> None:
    company_name = "武汉市网页设计公司0"
    gateway = FakeDoubaoWebGateway()
    gateway.public_search_results = [
        {
            "title": company_name,
            "text": f"{company_name} 联系电话 13800138000 武汉市",
            "url": "https://evidence.example/contact-0",
        }
    ]
    company_verifier = RecordingCompanyNameVerifier({company_name: "unverified"})
    service = CollectionService(
        TaskStore(tmp_path),
        {"web_search": EmptyProvider()},
        company_name_verifier=company_verifier,
        doubao_web_gateway=gateway,
    )
    task = service.create_task(
        {
            **_payload([]),
            "cities": ["武汉市"],
            "use_doubao_web_chat": True,
            "web_chat_discovery": True,
            "limit_per_city": 1,
        }
    )
    task["completed_city_discovery"] = ["武汉市"]
    task["leads"] = [
        {
            "city": "武汉市",
            "company_name": company_name,
            "source_url": "https://evidence.example/company-0",
            "company_name_evidence_urls": ["https://evidence.example/company-0"],
            "candidate_reason": "武汉市公开页面显示其提供别墅全案设计",
            "company_name_verification_status": "unverified",
            "enrichment_state": "pending",
        }
    ]
    service.store.save(task)

    try:
        service._run_two_stage_task(task, finalize=False, round_limit=1)
        enriched = service.get_task(task["id"])["leads"][0]
    finally:
        service.shutdown()

    assert company_verifier.calls == Counter()
    assert gateway.enrichment_calls == [company_name]
    assert enriched["mobile"] == "13800138000"
    assert enriched["company_name_verification_status"] == "verified_public_web"


def test_web_chat_enrichment_only_round_skips_new_company_discovery(
    tmp_path: Path,
) -> None:
    company_name = "武汉市已有候选设计公司0"
    gateway = FakeDoubaoWebGateway(discovery_batches=[[99]])
    service = CollectionService(
        TaskStore(tmp_path),
        {"web_search": EmptyProvider()},
        doubao_web_gateway=gateway,
    )
    task = service.create_task(
        {
            **_payload([]),
            "cities": ["武汉市"],
            "use_doubao_web_chat": True,
            "web_chat_discovery": True,
            "limit_per_city": 1,
        }
    )
    task["leads"] = [
        {
            "city": "武汉市",
            "company_name": company_name,
            "source_url": "https://evidence.example/company-existing",
            "company_name_evidence_urls": ["https://evidence.example/company-existing"],
            "candidate_reason": "武汉市公开页面显示其提供别墅全案设计",
            "company_name_verification_status": "unverified",
            "enrichment_state": "pending",
        }
    ]
    service.store.save(task)

    try:
        service._run_two_stage_task(
            task,
            finalize=False,
            round_limit=1,
            skip_discovery=True,
        )
        enriched = service.get_task(task["id"])["leads"][0]
    finally:
        service.shutdown()

    assert gateway.discovery_calls == []
    assert gateway.enrichment_calls == [company_name]
    assert enriched["mobile"] == "13800138000"


def test_web_chat_discovery_caps_batches_without_echoing_previous_companies(
    tmp_path: Path,
) -> None:
    gateway = FakeDoubaoWebGateway(
        discovery_batches=[list(range(7)), [0, 1, 2, 3, 5, 6]]
    )
    payload = {**_payload(["deepseek"]), "use_doubao_web_chat": True, "web_chat_discovery": True, "limit_per_city": 6}
    service = CollectionService(
        TaskStore(tmp_path),
        {"web_search": EmptyProvider()},
        {"deepseek": TwoStageVerifier()},
        doubao_web_gateway=gateway,
    )
    task = service.create_task(payload)
    try:
        service.prepare_start(task["id"])
        service.run_task(task["id"])
        completed = service.get_task(task["id"])

        assert gateway.discovery_calls == ["武汉市", "武汉市"]
        assert len(completed["leads"]) == 6
        assert completed["leads"][0]["company_name_evidence_urls"] == [
            "https://example.test/company-0"
        ]
        assert "武汉市网页设计公司0" not in gateway.discovery_requests[1]
        assert "自动去重" in gateway.discovery_requests[1]
        assert [lead["company_name"] for lead in completed["leads"]][-1] == (
            "武汉市网页设计公司5"
        )
    finally:
        service.shutdown()


def test_web_chat_discovery_continues_after_a_duplicate_batch(
    tmp_path: Path,
) -> None:
    gateway = FakeDoubaoWebGateway(
        discovery_batches=[list(range(5)), list(range(5)), list(range(5, 10))]
    )
    service = CollectionService(
        TaskStore(tmp_path),
        {"web_search": EmptyProvider()},
        doubao_web_gateway=gateway,
    )
    task = service.create_task(
        {
            **_payload([]),
            "use_doubao_web_chat": True,
            "web_chat_discovery": True,
            "limit_per_city": 11,
        }
    )

    try:
        service.prepare_start(task["id"])
        service.run_task(task["id"])
        completed = service.get_task(task["id"])
    finally:
        service.shutdown()

    assert len(gateway.discovery_calls) == 3
    assert len(completed["leads"]) == 10


def test_web_chat_discovery_continues_after_a_short_duplicate_batch(
    tmp_path: Path,
) -> None:
    gateway = FakeDoubaoWebGateway(
        discovery_batches=[[0], [0], [1, 2, 3, 4]]
    )
    service = CollectionService(
        TaskStore(tmp_path),
        {"web_search": EmptyProvider()},
        doubao_web_gateway=gateway,
    )
    task = service.create_task(
        {
            **_payload([]),
            "use_doubao_web_chat": True,
            "web_chat_discovery": True,
            "limit_per_city": 5,
        }
    )

    try:
        service.prepare_start(task["id"])
        service.run_task(task["id"])
        completed = service.get_task(task["id"])
    finally:
        service.shutdown()

    assert len(gateway.discovery_calls) == 3
    assert len(completed["leads"]) == 5


def test_web_chat_discovery_continues_after_a_full_duplicate_batch(
    tmp_path: Path,
) -> None:
    gateway = FakeDoubaoWebGateway(
        discovery_batches=[list(range(5)), list(range(5)), [5]]
    )
    service = CollectionService(
        TaskStore(tmp_path),
        {"web_search": EmptyProvider()},
        doubao_web_gateway=gateway,
    )
    task = service.create_task(
        {
            **_payload([]),
            "use_doubao_web_chat": True,
            "web_chat_discovery": True,
            "limit_per_city": 6,
        }
    )

    try:
        service.prepare_start(task["id"])
        service.run_task(task["id"])
        completed = service.get_task(task["id"])
    finally:
        service.shutdown()

    assert len(gateway.discovery_calls) == 3
    assert len(completed["leads"]) == 6


def test_web_chat_discovery_resume_keeps_short_batch_extension_budget(
    tmp_path: Path,
) -> None:
    gateway = FakeDoubaoWebGateway(
        discovery_batches=[[0], [1, 2, 3, 4]]
    )
    store = TaskStore(tmp_path)
    service = CollectionService(
        store,
        {"web_search": EmptyProvider()},
        doubao_web_gateway=gateway,
    )
    task = service.create_task(
        {
            **_payload([]),
            "use_doubao_web_chat": True,
            "web_chat_discovery": True,
            "limit_per_city": 5,
        }
    )
    task["leads"] = [
        normalize_lead(
            {"company_name": "武汉市网页设计公司0"},
            city="武汉市",
            provider="doubao_web",
        )
    ]
    task["web_chat_checkpoint"] = {
        "city": "武汉市",
        "batch_number": 1,
        "round_start_candidate_count": 0,
        "max_batches": 3,
        "short_batch_extensions": 0,
        "response_error_seen": False,
    }
    store.save(task)

    try:
        service._discover_city_candidates_web(task, "武汉市", 5)
    finally:
        service.shutdown()

    assert len(gateway.discovery_calls) == 2
    assert len(task["leads"]) == 5


def test_web_chat_discovery_migrates_legacy_checkpoint_budget(tmp_path: Path) -> None:
    gateway = FakeDoubaoWebGateway(discovery_batches=[[1]])
    store = TaskStore(tmp_path)
    service = CollectionService(
        store,
        {"web_search": EmptyProvider()},
        doubao_web_gateway=gateway,
    )
    task = service.create_task(
        {
            **_payload(["deepseek"]),
            "cities": ["Shenzhen"],
            "use_doubao_web_chat": True,
            "web_chat_discovery": True,
            "limit_per_city": 5,
        }
    )
    task["leads"] = [
        normalize_lead(
            {"company_name": "Existing Design Co"},
            city="Shenzhen",
            provider="doubao_web",
        )
    ]
    task["web_chat_checkpoint"] = {
        "city": "Shenzhen",
        "batch_number": 1,
    }
    store.save(task)

    try:
        service._discover_city_candidates_web(task, "Shenzhen", 5)
    finally:
        service.shutdown()

    assert gateway.discovery_calls == ["Shenzhen"]
    assert len(task["leads"]) == 2
    assert task["web_chat_checkpoint"]["max_batches"] == 2


def test_candidate_persistence_reserves_company_across_nonterminal_tasks(
    tmp_path: Path,
) -> None:
    service = CollectionService(TaskStore(tmp_path), {"web_search": EmptyProvider()})
    first = service.create_task(_payload(["doubao"]))
    second = service.create_task(_payload(["doubao"]))
    try:
        service._persist_discovered_candidates(
            first,
            "深圳市",
            [{"company_name": "深圳市跨任务重复设计有限公司"}],
            10,
        )
        service._persist_discovered_candidates(
            second,
            "深圳市",
            [
                {"company_name": "深圳市跨任务重复设计有限公司"},
                {"company_name": "深圳市跨任务新设计有限公司"},
            ],
            10,
        )

        assert [lead["company_name"] for lead in second["leads"]] == [
            "深圳市跨任务新设计有限公司"
        ]
    finally:
        service.shutdown()


def test_candidate_reason_mobile_is_preserved_for_later_verification(
    tmp_path: Path,
) -> None:
    service = CollectionService(TaskStore(tmp_path), {"web_search": EmptyProvider()})
    task = service.create_task(_payload(["doubao"]))
    try:
        service._persist_discovered_candidates(
            task,
            "武汉市",
            [
                {
                    "company_name": "武汉市来源软装有限公司",
                    "reason": "公开来源写明业务手机号 13800138000",
                    "source_url": "https://example.test/company",
                }
            ],
            1,
        )
        lead = service.get_task(task["id"])["leads"][0]
    finally:
        service.shutdown()

    assert lead["mobile"] == "13800138000"
    assert lead["contact_verification_status"] == "unverified"
    assert lead["source_url"] == "https://example.test/company"


def test_legacy_candidate_contacts_are_hydrated_when_task_is_read(
    tmp_path: Path,
) -> None:
    service = CollectionService(TaskStore(tmp_path), {"web_search": EmptyProvider()})
    task = service.create_task(_payload(["doubao"]))
    task["leads"] = [
        {
            "city": "深圳市",
            "company_name": "深圳市历史候选设计有限公司",
            "candidate_reason": "公开业务手机号 13800138000，邮箱 history@example.com",
            "mobile": None,
            "email": None,
            "contact_verification_status": "unverified",
        }
    ]
    service.store.save(task)
    try:
        loaded = service.get_task(task["id"])
        persisted = service.store.load(task["id"])
    finally:
        service.shutdown()

    assert loaded["leads"][0]["mobile"] == "13800138000"
    assert loaded["leads"][0]["email"] == "history@example.com"
    assert persisted["leads"][0]["mobile"] == "13800138000"
    assert persisted["leads"][0]["email"] == "history@example.com"
    assert loaded["leads"][0]["contact_verification_status"] == "unverified"


def test_web_enrichment_prioritizes_untried_candidates_before_failed_retries(
    tmp_path: Path,
) -> None:
    failed_company = "深圳市先前失败设计有限公司"
    pending_company = "深圳市新候选设计有限公司"

    class OrderedGateway(FakeDoubaoWebGateway):
        def enrich_company(self, **kwargs: Any) -> DoubaoWebContact:
            if kwargs["company_name"] == failed_company:
                self.enrichment_calls.append(failed_company)
                raise DoubaoWebResponseError("模拟响应解析失败")
            return super().enrich_company(**kwargs)

    gateway = OrderedGateway()
    service = CollectionService(
        TaskStore(tmp_path),
        {"web_search": EmptyProvider()},
        doubao_web_gateway=gateway,
    )
    task = service.create_task({**_payload([]), "use_doubao_web_chat": True})
    task["leads"] = [
        {
            **normalize_lead(
                {"company_name": failed_company},
                city="深圳市",
                provider="doubao_web",
            ),
            "enrichment_state": "failed",
        },
        {
            **normalize_lead(
                {"company_name": pending_company},
                city="深圳市",
                provider="doubao_web",
            ),
            "enrichment_state": "pending",
        },
    ]
    service.store.save(task)
    try:
        service._enrich_city_candidates_web(task, "深圳市")
    finally:
        service.shutdown()

    assert gateway.enrichment_calls[:3] == [pending_company, failed_company, failed_company]


def test_web_enrichment_retries_pending_checkpoint_after_restart(tmp_path: Path) -> None:
    company_name = "深圳市重启后待补全设计有限公司"
    gateway = FakeDoubaoWebGateway()
    service = CollectionService(
        TaskStore(tmp_path),
        {"web_search": EmptyProvider()},
        doubao_web_gateway=gateway,
    )
    task = service.create_task({**_payload([]), "use_doubao_web_chat": True})
    task["leads"] = [
        {
            **normalize_lead(
                {"company_name": company_name},
                city="深圳市",
                provider="doubao_web",
            ),
            "enrichment_state": "pending",
        }
    ]
    task["web_chat_checkpoint"] = {
        "city": "深圳市",
        "company_index": 1,
        "processed_normalized_names": [company_name],
    }
    service.store.save(task)
    try:
        service._enrich_city_candidates_web(task, "深圳市")
    finally:
        service.shutdown()

    assert gateway.enrichment_calls == [company_name]


def test_hybrid_discovery_respects_existing_city_limit(
    tmp_path: Path,
) -> None:
    class PublicCandidateProvider(EmptyProvider):
        def collect(
            self, *, city: str, keyword: str, request_text: str
        ) -> list[dict[str, Any]]:
            del request_text
            return [{
                "company_name": f"{city}{keyword}公开企业",
                "mobile": "13800138000",
                "source_url": f"https://public.example/{keyword}",
                "matched_keyword": keyword,
            }]

    gateway = FakeDoubaoWebGateway(discovery_batches=[[8, 9, 10]])
    service = CollectionService(
        TaskStore(tmp_path),
        {"web_search": PublicCandidateProvider()},
        doubao_web_gateway=gateway,
    )
    task = service.create_task(
        {
            **_payload([]),
            "use_doubao_web_chat": True,
            "web_chat_discovery": True,
            "limit_per_city": 3,
        }
    )
    task["leads"] = [
        normalize_lead(
            {"company_name": f"武汉市既有公司{i}"},
            city="武汉市",
            provider="web_search",
        )
        for i in range(2)
    ]
    service.store.save(task)

    try:
        service._discover_city_candidates(task, "武汉市", 3)
    finally:
        service.shutdown()

    assert len(task["leads"]) == 3


def test_web_chat_discovery_uses_doubao_before_public_web_fallback(
    tmp_path: Path,
) -> None:
    class PublicCandidateProvider(EmptyProvider):
        def __init__(self) -> None:
            self.calls: list[str] = []

        def collect(
            self, *, city: str, keyword: str, request_text: str
        ) -> list[dict[str, Any]]:
            del request_text
            self.calls.append(keyword)
            return [{
                "company_name": f"{city}{keyword}公开企业",
                "mobile": "13800138000" if keyword == "软装公司" else "13900139000",
                "source_url": f"https://public.example/{keyword}",
                "matched_keyword": keyword,
            }]

    gateway = FakeDoubaoWebGateway(discovery_batches=[[8, 9]])
    public_provider = PublicCandidateProvider()
    service = CollectionService(
        TaskStore(tmp_path),
        {"web_search": public_provider},
        doubao_web_gateway=gateway,
    )
    task = service.create_task(
        {
            **_payload([]),
            "use_doubao_web_chat": True,
            "web_chat_discovery": True,
            "limit_per_city": 3,
        }
    )

    try:
        service._discover_city_candidates(task, "武汉市", 3)
    finally:
        service.shutdown()

    names = {lead["company_name"] for lead in task["leads"]}
    assert "武汉市网页设计公司8" in names
    assert "武汉市网页设计公司9" in names
    assert "武汉市软装公司公开企业" in names
    assert len(task["leads"]) == 3
    assert gateway.discovery_calls
    assert set(gateway.discovery_calls) == {"武汉市"}
    assert public_provider.calls == ["软装公司"]


def test_successful_web_chat_batch_clears_previous_fallback_error(
    tmp_path: Path,
) -> None:
    gateway = FakeDoubaoWebGateway(discovery_batches=[[8, 9]])
    service = CollectionService(
        TaskStore(tmp_path),
        {"web_search": EmptyProvider()},
        doubao_web_gateway=gateway,
    )
    task = service.create_task(
        {
            **_payload([]),
            "use_doubao_web_chat": True,
            "web_chat_discovery": True,
            "limit_per_city": 2,
        }
    )
    task["web_chat_error"] = "豆包网页查询未完成，已回退原有公开网页逻辑：豆包网页浏览器未启动"

    try:
        service._discover_city_candidates_web(task, "武汉市", 2)
    finally:
        service.shutdown()

    assert task["web_chat_error"] is None


def test_web_chat_discovery_clears_stale_timeout_while_waiting(
    tmp_path: Path,
) -> None:
    class BlockingDiscoveryGateway(FakeDoubaoWebGateway):
        def __init__(self) -> None:
            super().__init__(discovery_batches=[[8, 9]])
            self.started = Event()
            self.release = Event()

        def discover_companies(self, **kwargs: Any) -> list[DoubaoWebCandidate]:
            self.started.set()
            self.release.wait(timeout=3)
            return super().discover_companies(**kwargs)

    gateway = BlockingDiscoveryGateway()
    service = CollectionService(
        TaskStore(tmp_path),
        {"web_search": EmptyProvider()},
        doubao_web_gateway=gateway,
    )
    task = service.create_task(
        {
            **_payload([]),
            "use_doubao_web_chat": True,
            "web_chat_discovery": True,
            "limit_per_city": 2,
        }
    )
    task["web_chat_error"] = "豆包网页查询未完成，已回退原有公开网页逻辑：豆包网页操作超时"

    worker = Thread(target=service._discover_city_candidates_web, args=(task, "武汉市", 2))
    try:
        worker.start()
        assert gateway.started.wait(timeout=1)
        assert task["web_chat_error"] is None
        gateway.release.set()
        worker.join(timeout=3)
        assert not worker.is_alive()
    finally:
        gateway.release.set()
        service.shutdown()


def test_web_chat_discovery_skips_one_malformed_batch_after_bounded_retries(
    tmp_path: Path,
) -> None:
    class MalformedFirstBatchGateway(FakeDoubaoWebGateway):
        def __init__(self) -> None:
            super().__init__(discovery_batches=[[5]])
            self.attempts = 0

        def discover_companies(self, **kwargs: Any) -> list[DoubaoWebCandidate]:
            self.attempts += 1
            if self.attempts <= 3:
                raise DoubaoWebResponseError("豆包返回中没有可解析的公司证据")
            return super().discover_companies(**kwargs)

    gateway = MalformedFirstBatchGateway()
    service = CollectionService(
        TaskStore(tmp_path),
        {"web_search": EmptyProvider()},
        doubao_web_gateway=gateway,
    )
    task = service.create_task(
        {
            **_payload([]),
            "use_doubao_web_chat": True,
            "web_chat_discovery": True,
            "limit_per_city": 10,
        }
    )

    try:
        service._discover_city_candidates_web(task, "武汉市", 10)
    finally:
        service.shutdown()

    assert gateway.attempts == 4
    assert gateway.discovery_calls == ["武汉市"]
    assert task["candidate_count"] == 1
    assert task["web_chat_checkpoint"]["batch_number"] == 2
    failures = [
        item
        for item in task["activity"]
        if item["action"] == "doubao_web_discovery_batch"
        and item["outcome"] == "error"
    ]
    assert len(failures) == 1


def test_web_chat_discovery_falls_back_to_public_search_after_timeout(
    tmp_path: Path,
) -> None:
    class TimeoutDiscoveryGateway(FakeDoubaoWebGateway):
        def discover_companies(self, **kwargs: Any) -> list[DoubaoWebCandidate]:
            self.discovery_calls.append(str(kwargs["city"]))
            raise DoubaoWebTimeout("等待豆包网页回答超时")

    gateway = TimeoutDiscoveryGateway()
    service = CollectionService(
        TaskStore(tmp_path),
        {"web_search": LegacyProvider()},
        doubao_web_gateway=gateway,
    )
    task = service.create_task(
        {
            **_payload([]),
            "use_doubao_web_chat": True,
            "web_chat_discovery": True,
            "limit_per_city": 1,
        }
    )

    try:
        service._discover_city_candidates_web(task, "武汉市", 1)
    finally:
        service.shutdown()

    assert task["status"] != "completed_with_errors"
    assert task["web_chat_status"] == "ready"
    assert "回退原有公开网页逻辑" in task["web_chat_error"]
    assert task["leads"][0]["company_name"] == "武汉市软装公司服务中心"
    assert any(
        item["action"] == "doubao_web_discovery_fallback"
        and item["outcome"] == "fallback"
        for item in task["activity"]
    )


def test_web_chat_public_fallback_is_bounded_per_round(tmp_path: Path) -> None:
    class RecordingFallbackProvider(LegacyProvider):
        def __init__(self) -> None:
            self.calls: list[str] = []

        def collect(
            self, *, city: str, keyword: str, request_text: str
        ) -> list[dict[str, Any]]:
            self.calls.append(keyword)
            return super().collect(
                city=city,
                keyword=keyword,
                request_text=request_text,
            )

    class TimeoutDiscoveryGateway(FakeDoubaoWebGateway):
        def discover_companies(self, **kwargs: Any) -> list[DoubaoWebCandidate]:
            self.discovery_calls.append(str(kwargs["city"]))
            raise DoubaoWebTimeout("等待豆包网页回答超时")

    provider = RecordingFallbackProvider()
    gateway = TimeoutDiscoveryGateway()
    service = CollectionService(
        TaskStore(tmp_path),
        {"web_search": provider},
        doubao_web_gateway=gateway,
    )
    task = service.create_task(
        {
            **_payload([]),
            "keywords": [f"关键词{i}" for i in range(8)],
            "use_doubao_web_chat": True,
            "web_chat_discovery": True,
            "limit_per_city": 100,
        }
    )

    try:
        service._discover_city_candidates(task, "武汉市", 100)
    finally:
        service.shutdown()

    assert len(provider.calls) == 6
    assert provider.calls == task["keywords"][:6]


def test_web_chat_discovery_backs_off_after_timeout_before_retrying_doubao(
    tmp_path: Path,
) -> None:
    class RecordingFallbackProvider(LegacyProvider):
        def __init__(self) -> None:
            self.calls: list[str] = []

        def collect(
            self, *, city: str, keyword: str, request_text: str
        ) -> list[dict[str, Any]]:
            self.calls.append(keyword)
            return super().collect(
                city=city,
                keyword=keyword,
                request_text=request_text,
            )

    class TimeoutDiscoveryGateway(FakeDoubaoWebGateway):
        def discover_companies(self, **kwargs: Any) -> list[DoubaoWebCandidate]:
            self.discovery_calls.append(str(kwargs["city"]))
            raise DoubaoWebTimeout("等待豆包网页回答超时")

    provider = RecordingFallbackProvider()
    gateway = TimeoutDiscoveryGateway()
    service = CollectionService(
        TaskStore(tmp_path),
        {"web_search": provider},
        doubao_web_gateway=gateway,
    )
    task = service.create_task(
        {
            **_payload([]),
            "keywords": [f"关键词{i}" for i in range(8)],
            "use_doubao_web_chat": True,
            "web_chat_discovery": True,
            "limit_per_city": 100,
        }
    )

    try:
        service._discover_city_candidates(task, "武汉市", 100)
        service._discover_city_candidates(task, "武汉市", 100)
    finally:
        service.shutdown()

    assert gateway.discovery_calls == ["武汉市"]
    assert len(provider.calls) == 12
    assert task["web_chat_discovery_failures"] == 1
    assert task["web_chat_discovery_retry_after"]


def test_web_chat_discovery_resumes_from_checkpoint_batch(tmp_path: Path) -> None:
    gateway = FakeDoubaoWebGateway(discovery_batches=[[5]])
    payload = {**_payload(["deepseek"]), "use_doubao_web_chat": True, "web_chat_discovery": True, "limit_per_city": 6}
    store = TaskStore(tmp_path)
    service = CollectionService(
        store,
        {"web_search": EmptyProvider()},
        {"deepseek": TwoStageVerifier()},
        doubao_web_gateway=gateway,
    )
    task = service.create_task(payload)
    try:
        store.update(
            task["id"],
            lambda current: current["web_chat_checkpoint"].update(
                city="武汉市", batch_number=1
            ),
        )
        service.prepare_start(task["id"])
        service.run_task(task["id"])
        completed = service.get_task(task["id"])

        assert gateway.discovery_calls == ["武汉市"]
        assert "第 2 批" in gateway.discovery_requests[0]
        assert completed["web_chat_checkpoint"]["batch_number"] == 2
    finally:
        service.shutdown()


def test_continuous_web_chat_discovery_collects_a_fresh_limit_each_round(
    tmp_path: Path,
) -> None:
    class MultiRoundGateway(FakeDoubaoWebGateway):
        def enrich_company(self, **kwargs: Any) -> DoubaoWebContact:
            contact = super().enrich_company(**kwargs)
            call_index = len(self.enrichment_calls) - 1
            mobile = f"1380000{call_index:04d}"
            return DoubaoWebContact(
                company_name=contact.company_name,
                mobile=mobile,
                contact_person_name=contact.contact_person_name,
                contact_person_role=contact.contact_person_role,
                email=contact.email,
                wechat=contact.wechat,
                address=contact.address,
                service_intro=contact.service_intro,
                source_urls=contact.source_urls,
                evidence_quotes=[f"{contact.company_name} {mobile}"],
                raw_response=contact.raw_response,
            )

    gateway = MultiRoundGateway(
        discovery_batches=[
            list(range(5)),
            [5],
            list(range(6, 11)),
            [11],
        ]
    )
    payload = {
        **_payload(["deepseek"]),
        "use_doubao_web_chat": True,
        "web_chat_discovery": True,
        "continuous_mode": True,
        "limit_per_city": 6,
    }
    service = CollectionService(
        TaskStore(tmp_path),
        {"web_search": EmptyProvider()},
        {"deepseek": TwoStageVerifier()},
        doubao_web_gateway=gateway,
    )
    task = service.create_task(payload)
    try:
        service._run_two_stage_task(task, finalize=False, round_limit=6)
        service._prepare_next_round(task["id"])
        next_round = service.store.load(task["id"])

        service._run_two_stage_task(next_round, finalize=False, round_limit=6)
        completed = service.get_task(task["id"])

        assert gateway.discovery_calls == ["武汉市"] * 4
        assert len(completed["leads"]) == 12
        assert completed["web_chat_checkpoint"]["batch_number"] == 2
    finally:
        service.shutdown()


def test_web_chat_discovery_honors_pause_before_next_browser_batch(
    tmp_path: Path,
) -> None:
    gateway = FakeDoubaoWebGateway(discovery_batches=[list(range(5)), list(range(5, 10))])
    payload = {**_payload(["deepseek"]), "use_doubao_web_chat": True, "web_chat_discovery": True, "limit_per_city": 10}
    service = CollectionService(
        TaskStore(tmp_path),
        {"web_search": EmptyProvider()},
        {"deepseek": TwoStageVerifier()},
        doubao_web_gateway=gateway,
    )
    task = service.create_task(payload)
    gateway.pause_after_discovery = lambda: service.pause_task(task["id"])
    try:
        service.prepare_start(task["id"])
        service.run_task(task["id"])
        paused = service.get_task(task["id"])

        assert paused["status"] == "paused"
        assert gateway.discovery_calls == ["武汉市"]
        assert len(paused["leads"]) == 5
        assert paused["web_chat_checkpoint"]["batch_number"] == 1
    finally:
        service.shutdown()


def test_waiting_for_login_task_rejects_pause(tmp_path: Path) -> None:
    gateway = FakeDoubaoWebGateway(interrupt_company="武汉市网页设计公司1")
    payload = {**_payload(["deepseek"]), "use_doubao_web_chat": True, "web_chat_discovery": True, "limit_per_city": 2}
    service = CollectionService(
        TaskStore(tmp_path),
        {"web_search": EmptyProvider()},
        {"deepseek": TwoStageVerifier()},
        doubao_web_gateway=gateway,
    )
    task = service.create_task(payload)
    try:
        service.prepare_start(task["id"])
        service.run_task(task["id"])

        with pytest.raises(InvalidTaskStateError, match="waiting for Doubao login"):
            service.pause_task(task["id"])
        assert service.get_task(task["id"])["status"] == "waiting_for_login"
    finally:
        service.shutdown()


def test_visible_challenge_records_challenge_reason_separately_from_login(
    tmp_path: Path,
) -> None:
    gateway = FakeDoubaoWebGateway(
        interrupt_company="武汉市网页设计公司1",
        interrupt_error=DoubaoChallengeRequired,
    )
    payload = {**_payload(["deepseek"]), "use_doubao_web_chat": True, "web_chat_discovery": True, "limit_per_city": 2}
    service = CollectionService(
        TaskStore(tmp_path),
        {"web_search": EmptyProvider()},
        {"deepseek": TwoStageVerifier()},
        doubao_web_gateway=gateway,
    )
    task = service.create_task(payload)
    try:
        service.prepare_start(task["id"])
        service.run_task(task["id"])
        waiting = service.get_task(task["id"])

        assert waiting["status"] == "waiting_for_login"
        assert waiting["web_chat_status"] == "challenge"
        assert waiting["pause_reason"] == "doubao_challenge"
    finally:
        service.shutdown()


def test_terminate_from_web_chat_login_wait_keeps_collected_results(tmp_path: Path) -> None:
    gateway = FakeDoubaoWebGateway(interrupt_company="武汉市网页设计公司1")
    payload = {**_payload(["deepseek"]), "use_doubao_web_chat": True, "web_chat_discovery": True, "limit_per_city": 2}
    service = CollectionService(
        TaskStore(tmp_path),
        {"web_search": EmptyProvider()},
        {"deepseek": TwoStageVerifier()},
        doubao_web_gateway=gateway,
    )
    task = service.create_task(payload)
    try:
        service.prepare_start(task["id"])
        service.run_task(task["id"])
        assert service.get_task(task["id"])["status"] == "waiting_for_login"

        terminated = service.terminate_task(task["id"])

        assert terminated["status"] == "terminated"
        assert len(service.get_task(task["id"])["leads"]) == 2
        assert service.get_task(task["id"])["enrichment_completed"] == 1
    finally:
        service.shutdown()


def test_web_chat_login_checkpoint_resumes_current_company_without_repeating(
    tmp_path: Path,
) -> None:
    second_company = "武汉市网页设计公司1"
    gateway = FakeDoubaoWebGateway(interrupt_company=second_company)
    payload = {**_payload(["deepseek"]), "use_doubao_web_chat": True, "web_chat_discovery": True, "limit_per_city": 2}
    service = CollectionService(
        TaskStore(tmp_path),
        {"web_search": EmptyProvider()},
        {"deepseek": TwoStageVerifier()},
        doubao_web_gateway=gateway,
        max_concurrent_tasks=1,
    )
    task = service.create_task(payload)
    try:
        service.prepare_start(task["id"])
        service.run_task(task["id"])
        waiting = service.get_task(task["id"])
        assert waiting["status"] == "waiting_for_login"
        assert waiting["web_chat_current_company"] == second_company
        assert waiting["web_chat_checkpoint"]["company_index"] == 1

        gateway.status_failure = True
        with pytest.raises(InvalidTaskStateError, match="豆包网页尚未完成登录"):
            service.resume_task(task["id"])

        gateway.status_failure = False
        gateway.ready = True
        monitor = DoubaoAttentionMonitor(
            waiting_task_ids=lambda: [task["id"]]
            if service.get_task_metadata(task["id"])["status"] == "waiting_for_login"
            else [],
            refresh_status=gateway.refresh_status,
            resume_task=service.resume_task,
            poll_seconds=60,
            ready_confirmations=2,
        )
        monitor.check_once()
        assert service.get_task_metadata(task["id"])["status"] == "waiting_for_login"
        monitor.check_once()
        deadline = monotonic() + 3
        while monotonic() < deadline and service.get_task(task["id"])["status"] not in {
            "completed",
            "completed_with_errors",
        }:
            sleep(0.01)

        completed = service.get_task(task["id"])
        assert completed["status"] == "completed"
        assert gateway.enrichment_calls.count("武汉市网页设计公司0") == 1
        assert gateway.enrichment_calls.count(second_company) == 2
    finally:
        service.shutdown()


def _wait_for_task_completion(
    service: CollectionService,
    task_id: str,
    *,
    timeout: float = 3.0,
) -> bool:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if service.get_task(task_id)["status"] in {"completed", "completed_with_errors"}:
            return True
        sleep(0.01)
    return False


def test_two_stage_collection_saves_candidates_then_enriches_three_at_a_time(tmp_path: Path) -> None:
    store = RecordingStore(tmp_path)
    deepseek = TwoStageVerifier()
    doubao = TwoStageVerifier()
    doubao.id = "doubao"
    service = CollectionService(
        store,
        {"web_search": EmptyProvider()},
        {"deepseek": deepseek, "doubao": doubao},
    )
    task = service.create_task(_payload(["deepseek"]))

    service.prepare_start(task["id"])
    service.run_task(task["id"])
    completed = service.get_task(task["id"])

    assert deepseek.discovery_calls == [("武汉市", ["软装公司", "整装公司"])]
    assert completed["candidate_count"] == 4
    assert completed["enrichment_total"] == 4
    assert completed["enrichment_completed"] == 4
    assert len(completed["leads"]) == 4
    assert completed["leads"][0]["mobile"] is None
    assert doubao.max_active == 3
    assert any(
        snapshot.get("candidate_count") == 4 and snapshot.get("enrichment_completed") == 0
        for snapshot in store.snapshots
    )
    assert len(store.lead_batches[0]) == 4
    assert {lead["enrichment_state"] for lead in store.lead_batches[0]} == {"pending"}
    enrichment_batches = [
        batch
        for batch in store.lead_batches[1:]
        if len(batch) == 1 and "delivery_status" not in batch[0]
    ]
    assert len(enrichment_batches) == 4
    assert all(
        batch[0]["enrichment_state"] in {"completed", "failed"}
        for batch in enrichment_batches
    )


def test_company_name_evidence_gate_only_enriches_verified_candidates(
    tmp_path: Path,
) -> None:
    store = RecordingStore(tmp_path)
    deepseek = TwoStageVerifier()
    doubao = TwoStageVerifier()
    doubao.id = "doubao"
    statuses = {
        "武汉市真实装饰公司0": "verified_official_page",
        "武汉市真实装饰公司1": "unverified",
        "武汉市真实装饰公司2": "rejected",
        "武汉市真实装饰公司3": "verification_failed",
    }
    company_verifier = RecordingCompanyNameVerifier(statuses)
    service = CollectionService(
        store,
        {"web_search": EmptyProvider()},
        {"deepseek": deepseek, "doubao": doubao},
        company_name_verifier=company_verifier,
    )
    task = service.create_task(_payload(["deepseek"]))

    service.prepare_start(task["id"])
    service.run_task(task["id"])
    completed = service.get_task(task["id"])

    assert doubao.enrichment_calls == ["武汉市真实装饰公司0"]
    assert company_verifier.max_active == 3
    assert all(count == 1 for count in company_verifier.calls.values())
    assert company_verifier.source_urls["武汉市真实装饰公司0"] == []
    persisted = {lead["company_name"]: lead for lead in completed["leads"]}
    assert persisted["武汉市真实装饰公司0"]["company_name_verification_status"] == (
        "verified_official_page"
    )
    assert persisted["武汉市真实装饰公司0"]["company_name_evidence_urls"] == [
        "https://evidence.example/0"
    ]
    assert persisted["武汉市真实装饰公司1"]["enrichment_state"] == "blocked"
    assert persisted["武汉市真实装饰公司2"]["enrichment_state"] == "blocked"
    assert persisted["武汉市真实装饰公司3"]["enrichment_state"] == "blocked"
    assert all(lead["company_name_verified_at"] for lead in persisted.values())

    service._verify_city_company_names(completed, "武汉市")

    assert all(count == 1 for count in company_verifier.calls.values())


def test_web_chat_enriches_source_backed_candidates_without_name_gate(
    tmp_path: Path,
) -> None:
    events: list[str] = []

    class EventCompanyVerifier(RecordingCompanyNameVerifier):
        def verify_company_name(self, **kwargs: Any) -> CompanyNameEvidence:
            events.append(f"verify:{kwargs['company_name']}")
            return super().verify_company_name(**kwargs)

    class EventGateway(FakeDoubaoWebGateway):
        def enrich_company(self, **kwargs: Any) -> DoubaoWebContact:
            events.append(f"enrich:{kwargs['company_name']}")
            return super().enrich_company(**kwargs)

    names = [f"武汉市网页设计公司{i}" for i in range(25)]
    company_verifier = EventCompanyVerifier(
        {name: "verified_public_web" for name in names}
    )
    gateway = EventGateway(
        discovery_batches=[
            list(range(0, 5)),
            list(range(5, 10)),
            list(range(10, 15)),
            list(range(15, 20)),
            list(range(20, 25)),
        ]
    )
    service = CollectionService(
        TaskStore(tmp_path),
        {"web_search": EmptyProvider()},
        company_name_verifier=company_verifier,
        doubao_web_gateway=gateway,
    )
    task = service.create_task(
        {
            **_payload([]),
            "use_doubao_web_chat": True,
            "web_chat_discovery": True,
            "limit_per_city": 25,
        }
    )

    try:
        service._run_two_stage_task(task, finalize=False, round_limit=25)
    finally:
        service.shutdown()

    assert company_verifier.calls == Counter()
    assert len(gateway.enrichment_calls) == 25
    assert all(event.startswith("enrich:") for event in events)


def test_company_name_gate_forwards_discovery_source_urls(tmp_path: Path) -> None:
    verifier = RecordingCompanyNameVerifier(
        {"武汉市来源设计有限公司": "verified_official_page"}
    )
    service = CollectionService(
        TaskStore(tmp_path),
        {"web_search": EmptyProvider()},
        company_name_verifier=verifier,
    )
    task = service.create_task(_payload([]))
    task["leads"] = [
        {
            **normalize_lead(
            {
                "company_name": "武汉市来源设计有限公司",
                "source_url": "https://company.example/about",
            },
            city="武汉市",
            provider="doubao_web",
            ),
        }
    ]
    service.store.save(task)
    try:
        service._verify_city_company_names(task, "武汉市")

        assert verifier.source_urls["武汉市来源设计有限公司"] == [
            "https://company.example/about"
        ]
    finally:
        service.shutdown()


def test_company_name_gate_uses_browser_search_summary_fallback(tmp_path: Path) -> None:
    company_name = "深圳市真实空间设计有限公司"
    verifier = RecordingCompanyNameVerifier({company_name: "unverified"})
    gateway = FakeDoubaoWebGateway()
    gateway.public_search_results = [{
        "title": company_name,
        "url": "http://www.baidu.com/link?url=company",
        "text": f"{company_name}位于深圳，提供别墅全案设计和软装服务。",
    }]
    service = CollectionService(
        TaskStore(tmp_path),
        {"web_search": EmptyProvider()},
        company_name_verifier=verifier,
        doubao_web_gateway=gateway,
    )
    task = service.create_task(_payload([]))
    task["cities"] = ["深圳市"]
    task["leads"] = [
        normalize_lead(
            {"company_name": company_name},
            city="深圳市",
            provider="doubao_web",
        )
    ]
    service.store.save(task)

    try:
        service._verify_city_company_names(task, "深圳市")
    finally:
        service.shutdown()

    lead = task["leads"][0]
    assert lead["company_name_verification_status"] == "verified_public_web"
    assert lead["company_name_evidence_urls"] == [
        "http://www.baidu.com/link?url=company"
    ]
    assert gateway.public_search_calls


def test_contact_evidence_gate_preserves_unverified_mobile_for_display(
    tmp_path: Path,
) -> None:
    store = RecordingStore(tmp_path)
    deepseek = TwoStageVerifier()
    doubao = TwoStageVerifier()
    doubao.id = "doubao"
    statuses = {
        "武汉市真实装饰公司1": "verified_official_page",
        "武汉市真实装饰公司2": "rejected_mismatch",
        "武汉市真实装饰公司3": "verification_failed",
    }
    contact_verifier = RecordingContactVerifier(statuses)
    service = CollectionService(
        store,
        {"web_search": EmptyProvider()},
        {"deepseek": deepseek, "doubao": doubao},
        contact_verifier=contact_verifier,
    )
    task = service.create_task(_payload(["deepseek"]))

    service.prepare_start(task["id"])
    service.run_task(task["id"])
    leads = {lead["company_name"]: lead for lead in service.get_task(task["id"])["leads"]}

    verified = leads["武汉市真实装饰公司1"]
    assert verified["mobile"] == "13800138001"
    assert verified["contact_person_name"] == "张三"
    assert verified["contact_person_role"] == "业务负责人"
    assert verified["contact_verification_status"] == "verified_official_page"
    assert verified["contact_evidence_url"] == "https://example.test/1"
    assert verified["contact_verified_at"]

    assert leads["武汉市真实装饰公司2"]["mobile"] == "13800138002"
    assert leads["武汉市真实装饰公司2"]["contact_verification_status"] == "rejected_mismatch"
    assert leads["武汉市真实装饰公司3"]["mobile"] == "13800138003"
    assert (
        leads["武汉市真实装饰公司3"]["contact_verification_status"]
        == "verification_failed"
    )
    assert all(count == 1 for count in contact_verifier.calls.values())


def test_web_chat_checks_unverified_company_names_before_blocking_erp(
    tmp_path: Path,
) -> None:
    store = RecordingStore(tmp_path)
    gateway = FakeDoubaoWebGateway()
    company_verifier = RecordingCompanyNameVerifier(
        {
            "武汉市网页设计公司0": "unverified",
            "武汉市网页设计公司1": "verification_failed",
        }
    )
    contact_verifier = RecordingContactVerifier(
        {
            "武汉市网页设计公司0": "unverified_model_only",
            "武汉市网页设计公司1": "verification_failed",
        }
    )
    delivery = RecordingDelivery(store)
    service = CollectionService(
        store,
        {"web_search": EmptyProvider()},
        {"deepseek": TwoStageVerifier()},
        company_name_verifier=company_verifier,
        contact_verifier=contact_verifier,
        doubao_web_gateway=gateway,
        lead_delivery=delivery,
    )
    task = service.create_task(
        {
            **_payload(["deepseek"]),
            "use_doubao_web_chat": True,
            "web_chat_discovery": True,
            "verified_auto_deliver": True,
            "limit_per_city": 2,
        }
    )

    service.prepare_start(task["id"])
    service.run_task(task["id"])
    leads = service.get_task(task["id"])["leads"]

    assert gateway.enrichment_calls == [
        "武汉市网页设计公司0",
        "武汉市网页设计公司1",
    ]
    assert [lead["mobile"] for lead in leads] == ["13800138000", "13800138001"]
    assert {lead["contact_verification_status"] for lead in leads} == {
        "unverified_model_only",
        "verification_failed",
    }
    assert {lead["company_name_verification_status"] for lead in leads} == {
        "unverified",
    }
    assert {lead["delivery_status"] for lead in leads} == {"display_only"}
    assert {lead["enrichment_state"] for lead in leads} == {"completed"}
    assert company_verifier.calls == Counter()
    assert delivery.delivered == []


def test_web_chat_enriches_when_public_company_search_is_temporarily_unavailable(
    tmp_path: Path,
) -> None:
    company_name = "深圳市网页暂不可用设计公司0"
    gateway = FakeDoubaoWebGateway()
    service = CollectionService(
        TaskStore(tmp_path),
        {"web_search": EmptyProvider()},
        company_name_verifier=RecordingCompanyNameVerifier(
            {company_name: "verification_failed"}
        ),
        doubao_web_gateway=gateway,
    )
    task = service.create_task(
        {
            **_payload([]),
            "cities": ["深圳市"],
            "use_doubao_web_chat": True,
            "web_chat_discovery": True,
        }
    )
    task["leads"] = [
        {
            **normalize_lead(
                {"company_name": company_name},
                city="深圳市",
                provider="doubao_web",
            ),
            "company_name_verification_status": "unverified",
            "company_name_evidence_urls": [],
            "enrichment_state": "pending",
        }
    ]
    service.store.save(task)

    try:
        service._enrich_city_candidates_web(task, "深圳市")
    finally:
        service.shutdown()

    assert gateway.enrichment_calls == [company_name]
    assert task["leads"][0]["mobile"] == "13800138000"
    assert task["leads"][0]["enrichment_state"] == "completed"


def test_web_chat_enrichment_clears_stale_discovery_timeout_while_waiting(
    tmp_path: Path,
) -> None:
    company_name = "深圳市清理旧超时提示设计公司0"

    class BlockingEnrichmentGateway(FakeDoubaoWebGateway):
        def __init__(self) -> None:
            super().__init__()
            self.started = Event()
            self.release = Event()

        def enrich_company(self, **kwargs: Any) -> DoubaoWebContact:
            self.started.set()
            self.release.wait(timeout=3)
            return super().enrich_company(**kwargs)

    gateway = BlockingEnrichmentGateway()
    service = CollectionService(
        TaskStore(tmp_path),
        {"web_search": EmptyProvider()},
        company_name_verifier=RecordingCompanyNameVerifier(
            {company_name: "verification_failed"}
        ),
        doubao_web_gateway=gateway,
    )
    task = service.create_task(
        {
            **_payload([]),
            "cities": ["深圳市"],
            "use_doubao_web_chat": True,
            "web_chat_discovery": True,
        }
    )
    task["web_chat_error"] = "豆包网页查询未完成，已回退原有公开网页逻辑：豆包网页操作超时"
    task["leads"] = [
        {
            **normalize_lead(
                {"company_name": company_name},
                city="深圳市",
                provider="doubao_web",
            ),
            "enrichment_state": "pending",
        }
    ]
    service.store.save(task)

    worker = Thread(target=service._enrich_city_candidates_web, args=(task, "深圳市"))
    try:
        worker.start()
        assert gateway.started.wait(timeout=1)
        assert task["web_chat_error"] is None
        gateway.release.set()
        worker.join(timeout=3)
        assert not worker.is_alive()
    finally:
        gateway.release.set()
        service.shutdown()


def test_web_chat_persists_model_contact_before_evidence_verification(
    tmp_path: Path,
) -> None:
    company_name = "深圳市先展示手机号设计公司0"
    store = RecordingStore(tmp_path)
    service = CollectionService(
        store,
        {"web_search": EmptyProvider()},
        contact_verifier=RecordingContactVerifier(
            {company_name: "verification_failed"}
        ),
        doubao_web_gateway=FakeDoubaoWebGateway(),
    )
    task = service.create_task(
        {
            **_payload([]),
            "cities": ["深圳市"],
            "use_doubao_web_chat": True,
            "web_chat_discovery": True,
        }
    )
    task["leads"] = [
        {
            **normalize_lead(
                {"company_name": company_name},
                city="深圳市",
                provider="doubao_web",
            ),
            "enrichment_state": "pending",
        }
    ]
    service.store.save(task)

    try:
        service._enrich_city_candidates_web(task, "深圳市")
    finally:
        service.shutdown()

    batches = [
        batch[0]
        for batch in store.lead_batches
        if batch and batch[0].get("company_name") == company_name
    ]
    assert len(batches) >= 2
    assert batches[0]["mobile"] == "13800138000"
    assert batches[0]["contact_verification_status"] == "unverified"
    assert batches[-1]["contact_verification_status"] == "verification_failed"


def test_web_chat_keeps_collected_person_and_role_when_verification_fails(
    tmp_path: Path,
) -> None:
    company_name = "深圳市保留采集字段设计公司0"
    store = RecordingStore(tmp_path)
    gateway = FakeDoubaoWebGateway()
    original_enrich = gateway.enrich_company

    def enrich_with_person(**kwargs: Any) -> DoubaoWebContact:
        contact = original_enrich(**kwargs)
        return DoubaoWebContact(
            company_name=contact.company_name,
            mobile=contact.mobile,
            contact_person_name="罗震",
            contact_person_role="设计总监",
            email="luozhen@example.cn",
            wechat="luozhen_design",
            address=contact.address,
            service_intro=contact.service_intro,
            source_urls=contact.source_urls,
            evidence_quotes=contact.evidence_quotes,
            raw_response=contact.raw_response,
        )

    gateway.enrich_company = enrich_with_person  # type: ignore[method-assign]
    service = CollectionService(
        store,
        {"web_search": EmptyProvider()},
        contact_verifier=RecordingContactVerifier(
            {company_name: "verification_failed"}
        ),
        doubao_web_gateway=gateway,
    )
    task = service.create_task(
        {
            **_payload([]),
            "cities": ["深圳市"],
            "use_doubao_web_chat": True,
            "web_chat_discovery": True,
        }
    )
    task["leads"] = [
        {
            **normalize_lead(
                {"company_name": company_name},
                city="深圳市",
                provider="doubao_web",
            ),
            "enrichment_state": "pending",
        }
    ]
    service.store.save(task)

    try:
        service._enrich_city_candidates_web(task, "深圳市")
    finally:
        service.shutdown()

    lead = task["leads"][0]
    assert lead["mobile"] == "13800138000"
    assert lead["contact_person_name"] == "罗震"
    assert lead["contact_person_role"] == "设计总监"
    assert lead["email"] == "luozhen@example.cn"
    assert lead["wechat"] == "luozhen_design"
    assert lead["contact_verification_status"] == "verification_failed"
    assert lead["delivery_status"] == "display_only"


def test_web_chat_verifies_discovery_quote_mobile_before_contact_prompt(
    tmp_path: Path,
) -> None:
    store = RecordingStore(tmp_path)
    gateway = FakeDoubaoWebGateway()
    contact_verifier = RecordingContactVerifier(
        {"深圳市真实软装设计有限公司": "verified_public_web"}
    )
    service = CollectionService(
        store,
        {"web_search": EmptyProvider()},
        contact_verifier=contact_verifier,
        doubao_web_gateway=gateway,
    )
    task = service.create_task(
        {
            **_payload([]),
            "cities": ["深圳市"],
            "use_doubao_web_chat": True,
            "web_chat_discovery": True,
        }
    )
    task["leads"] = [
        {
            **normalize_lead(
                {
                    "company_name": "深圳市真实软装设计有限公司",
                    "source_url": "https://directory.example/company",
                },
                city="深圳市",
                provider="doubao_web",
            ),
            "candidate_reason": (
                "深圳市真实软装设计有限公司公开业务手机号 13800138001"
            ),
            "company_name_verification_status": "verified_public_web",
            "company_name_evidence_urls": ["https://directory.example/company"],
            "enrichment_state": "pending",
        }
    ]
    service.store.save(task)

    try:
        service._enrich_city_candidates_web(task, "深圳市")
    finally:
        service.shutdown()

    lead = task["leads"][0]
    assert gateway.enrichment_calls == []
    assert contact_verifier.calls["深圳市真实软装设计有限公司"] == 1
    assert lead["mobile"] == "13800138001"
    assert lead["contact_verification_status"] == "verified_public_web"
    assert lead["enrichment_state"] == "completed"


def test_web_chat_verifies_structured_public_web_mobile_before_contact_prompt(
    tmp_path: Path,
) -> None:
    contact_verifier = RecordingContactVerifier(
        {"深圳市公开网页设计有限公司": "verified_public_web"}
    )
    service = CollectionService(
        TaskStore(tmp_path),
        {"web_search": EmptyProvider()},
        contact_verifier=contact_verifier,
    )
    lead = normalize_lead(
        {
            "company_name": "深圳市公开网页设计有限公司",
            "mobile": "13800138001",
            "source_url": "https://directory.example/company",
        },
        city="深圳市",
        provider="web_search",
    )
    lead["company_name_evidence_urls"] = ["https://directory.example/company"]

    try:
        verified = service._verify_discovered_contact(lead)
    finally:
        service.shutdown()

    assert verified is True
    assert lead["mobile"] == "13800138001"
    assert lead["contact_verification_status"] == "verified_public_web"


def test_web_chat_verifies_structured_public_web_email_before_contact_prompt(
    tmp_path: Path,
) -> None:
    company_name = "深圳市邮箱设计有限公司"
    email = "sales@example.cn"
    contact_verifier = RecordingContactVerifier({company_name: "verified_public_web"})
    service = CollectionService(
        TaskStore(tmp_path),
        {"web_search": EmptyProvider()},
        contact_verifier=contact_verifier,
    )
    lead = normalize_lead(
        {
            "company_name": company_name,
            "email": email,
            "source_url": "https://directory.example/company",
        },
        city="深圳市",
        provider="web_search",
    )
    lead["company_name_evidence_urls"] = ["https://directory.example/company"]

    try:
        verified = service._verify_discovered_contact(lead)
    finally:
        service.shutdown()

    assert verified is True
    assert contact_verifier.calls[company_name] == 1
    assert lead["email"] == email
    assert lead["contact_verification_status"] == "verified_public_web"


def test_web_chat_uses_browser_search_to_verify_discovered_mobile(
    tmp_path: Path,
) -> None:
    company_name = "深圳市浏览器核验设计有限公司"
    mobile = "13800138001"
    gateway = FakeDoubaoWebGateway()
    gateway.public_search_results = [{
        "title": company_name,
        "url": "http://www.baidu.com/link?url=contact",
        "text": f"{company_name} 深圳别墅全案设计，业务手机 {mobile}",
    }]
    contact_verifier = RecordingContactVerifier(
        {company_name: "unverified_model_only"}
    )
    service = CollectionService(
        TaskStore(tmp_path),
        {"web_search": EmptyProvider()},
        contact_verifier=contact_verifier,
        doubao_web_gateway=gateway,
    )
    lead = normalize_lead(
        {
            "company_name": company_name,
            "mobile": mobile,
            "source_url": "https://directory.example/company",
        },
        city="深圳市",
        provider="doubao_web",
    )

    try:
        verified = service._verify_discovered_contact(lead)
    finally:
        service.shutdown()

    assert verified is True
    assert lead["contact_verification_status"] == "verified_public_web"
    assert lead["contact_evidence_url"] == "http://www.baidu.com/link?url=contact"
    assert company_name in lead["contact_evidence_quote"]
    assert mobile in lead["contact_evidence_quote"]


def test_web_chat_uses_browser_search_to_verify_enriched_mobile(
    tmp_path: Path,
) -> None:
    company_name = "深圳市豆包补全设计有限公司"
    mobile = "13800138001"

    class EnrichmentGateway(FakeDoubaoWebGateway):
        def enrich_company(self, **kwargs: Any) -> DoubaoWebContact:
            company = kwargs["company_name"]
            self.enrichment_calls.append(company)
            return DoubaoWebContact(
                company_name=company,
                mobile=mobile,
                contact_person_name=None,
                contact_person_role=None,
                email=None,
                wechat=None,
                address="深圳市公开地址",
                service_intro="别墅全案设计",
                source_urls=["https://doubao.example/contact"],
                evidence_quotes=[f"{company} 业务手机 {mobile}"],
                raw_response="raw",
            )

    gateway = EnrichmentGateway()
    gateway.public_search_results = [{
        "title": company_name,
        "url": "http://www.baidu.com/link?url=enriched-contact",
        "text": f"{company_name} 深圳别墅全案设计，业务手机 {mobile}",
    }]
    contact_verifier = RecordingContactVerifier(
        {company_name: "unverified_model_only"}
    )
    service = CollectionService(
        TaskStore(tmp_path),
        {"web_search": EmptyProvider()},
        contact_verifier=contact_verifier,
        doubao_web_gateway=gateway,
    )
    task = service.create_task(_payload([]))
    task["use_doubao_web_chat"] = True
    task["leads"] = [
        normalize_lead(
            {"company_name": company_name},
            city="深圳市",
            provider="doubao_web",
        )
    ]
    task["leads"][0]["enrichment_state"] = "pending"
    service.store.save(task)

    try:
        service._enrich_city_candidates_web(task, "深圳市")
    finally:
        service.shutdown()

    lead = task["leads"][0]
    assert gateway.enrichment_calls == [company_name]
    assert lead["mobile"] == mobile
    assert lead["contact_verification_status"] == "verified_public_web"
    assert lead["contact_evidence_url"] == (
        "http://www.baidu.com/link?url=enriched-contact"
    )


def test_web_chat_retries_a_parse_failure_and_continues_collection(
    tmp_path: Path,
) -> None:
    company_name = "深圳市豆包重试设计有限公司"

    class FlakyGateway(FakeDoubaoWebGateway):
        def __init__(self) -> None:
            super().__init__()
            self.failures = 1

        def enrich_company(self, **kwargs: Any) -> DoubaoWebContact:
            if self.failures:
                self.failures -= 1
                raise DoubaoWebResponseError("豆包联系方式返回格式无法解析")
            return super().enrich_company(**kwargs)

    gateway = FlakyGateway()
    service = CollectionService(
        TaskStore(tmp_path),
        {"web_search": EmptyProvider()},
        doubao_web_gateway=gateway,
    )
    task = service.create_task(_payload([]))
    task["use_doubao_web_chat"] = True
    task["leads"] = [
        {
            **normalize_lead(
                {"company_name": company_name},
                city="深圳市",
                provider="doubao_web",
            ),
            "enrichment_state": "pending",
        }
    ]
    service.store.save(task)

    try:
        service._enrich_city_candidates_web(task, "深圳市")
    finally:
        service.shutdown()

    assert gateway.enrichment_calls == [company_name]
    assert task["leads"][0]["enrichment_state"] == "completed"


def test_web_chat_reconnects_same_gateway_after_timeout_and_continues(
    tmp_path: Path,
) -> None:
    company_name = "深圳市豆包超时恢复设计有限公司"
    gateway = TimeoutThenRecoverGateway()
    service = CollectionService(
        TaskStore(tmp_path),
        {"web_search": EmptyProvider()},
        doubao_web_gateway=gateway,
        sleep=lambda _seconds: None,
    )
    task = service.create_task(_payload([]))
    task["use_doubao_web_chat"] = True
    task["leads"] = [
        {
            **normalize_lead(
                {"company_name": company_name},
                city="深圳市",
                provider="doubao_web",
            ),
            "enrichment_state": "pending",
        }
    ]
    service.store.save(task)

    try:
        service._enrich_city_candidates_web(task, "深圳市")
    finally:
        service.shutdown()

    assert gateway.reconnect_calls == 1
    assert gateway.enrichment_calls == [company_name]
    assert task["leads"][0]["enrichment_state"] == "completed"


def test_web_chat_skips_a_repeated_timeout_before_retrying_the_next_company(
    tmp_path: Path,
) -> None:
    first = "深圳市持续超时设计有限公司"
    second = "深圳市下一个设计有限公司"
    gateway = AlwaysTimeoutGateway()
    service = CollectionService(
        TaskStore(tmp_path),
        {"web_search": EmptyProvider()},
        doubao_web_gateway=gateway,
        sleep=lambda _seconds: None,
    )
    task = service.create_task(_payload([]))
    task["use_doubao_web_chat"] = True
    task["leads"] = [
        {
            **normalize_lead(
                {"company_name": first},
                city="深圳市",
                provider="doubao_web",
            ),
            "enrichment_state": "pending",
        },
        {
            **normalize_lead(
                {"company_name": second},
                city="深圳市",
                provider="doubao_web",
            ),
            "enrichment_state": "pending",
        },
    ]
    service.store.save(task)

    try:
        service._enrich_city_candidates_web(task, "深圳市")
    finally:
        service.shutdown()

    assert gateway.enrichment_calls == [first, first, second, second]
    assert all(lead["enrichment_state"] == "failed" for lead in task["leads"])


def test_web_chat_defers_a_failed_candidate_until_its_retry_window(
    tmp_path: Path,
) -> None:
    failed = "武汉市稍后重试设计有限公司"
    fresh = "武汉市先处理新候选设计有限公司"
    gateway = FakeDoubaoWebGateway()
    service = CollectionService(
        TaskStore(tmp_path),
        {"web_search": EmptyProvider()},
        doubao_web_gateway=gateway,
        sleep=lambda _seconds: None,
    )
    task = service.create_task(_payload([]))
    task["use_doubao_web_chat"] = True
    task["leads"] = [
        {
            **normalize_lead(
                {"company_name": failed, "source_url": "https://example.test/failed"},
                city="武汉市",
                provider="doubao_web",
            ),
            "enrichment_state": "failed",
            "enrichment_retry_after": "2099-01-01T00:00:00+00:00",
        },
        {
            **normalize_lead(
                {"company_name": fresh, "source_url": "https://example.test/fresh"},
                city="武汉市",
                provider="doubao_web",
            ),
            "enrichment_state": "pending",
        },
    ]
    service.store.save(task)

    try:
        service._enrich_city_candidates_web(task, "武汉市")
    finally:
        service.shutdown()

    assert gateway.enrichment_calls == [fresh]
    failed_lead = next(lead for lead in task["leads"] if lead["company_name"] == failed)
    assert failed_lead["enrichment_state"] == "failed"


def test_web_chat_migrates_legacy_timeout_failure_to_a_retry_window(
    tmp_path: Path,
) -> None:
    failed = "武汉市旧超时设计有限公司"
    fresh = "武汉市旧任务新候选设计有限公司"
    gateway = FakeDoubaoWebGateway()
    service = CollectionService(
        TaskStore(tmp_path),
        {"web_search": EmptyProvider()},
        doubao_web_gateway=gateway,
        sleep=lambda _seconds: None,
    )
    task = service.create_task(_payload([]))
    task["use_doubao_web_chat"] = True
    task["leads"] = [
        {
            **normalize_lead(
                {"company_name": failed, "source_url": "https://example.test/legacy"},
                city="武汉市",
                provider="doubao_web",
            ),
            "enrichment_state": "failed",
            "enrichment_error": "豆包网页操作超时",
        },
        {
            **normalize_lead(
                {"company_name": fresh, "source_url": "https://example.test/fresh"},
                city="武汉市",
                provider="doubao_web",
            ),
            "enrichment_state": "pending",
        },
    ]
    service.store.save(task)

    try:
        service._enrich_city_candidates_web(task, "武汉市")
    finally:
        service.shutdown()

    assert gateway.enrichment_calls == [fresh]
    failed_lead = next(lead for lead in task["leads"] if lead["company_name"] == failed)
    assert failed_lead["enrichment_retry_after"]


def test_web_chat_retries_a_previous_failed_candidate_on_next_pass(
    tmp_path: Path,
) -> None:
    company_name = "深圳市豆包跨轮重试设计有限公司"

    class FlakyGateway(FakeDoubaoWebGateway):
        def __init__(self) -> None:
            super().__init__()
            self.failures = 2

        def enrich_company(self, **kwargs: Any) -> DoubaoWebContact:
            if self.failures:
                self.failures -= 1
                raise DoubaoWebResponseError("豆包网页暂时不可用")
            return super().enrich_company(**kwargs)

    gateway = FlakyGateway()
    service = CollectionService(
        TaskStore(tmp_path),
        {"web_search": EmptyProvider()},
        doubao_web_gateway=gateway,
    )
    task = service.create_task(_payload([]))
    task["use_doubao_web_chat"] = True
    task["leads"] = [
        {
            **normalize_lead(
                {"company_name": company_name, "source_url": "https://example.test/company"},
                city="深圳市",
                provider="doubao_web",
            ),
            "enrichment_state": "pending",
        }
    ]
    service.store.save(task)

    try:
        service._enrich_city_candidates_web(task, "深圳市")
        assert task["leads"][0]["enrichment_state"] == "failed"
        service._enrich_city_candidates_web(task, "深圳市")
    finally:
        service.shutdown()

    assert gateway.enrichment_calls == [company_name]
    assert task["leads"][0]["enrichment_state"] == "completed"


def test_enrichment_always_uses_doubao_when_deepseek_is_selected_first(
    tmp_path: Path,
) -> None:
    store = RecordingStore(tmp_path)
    deepseek = TwoStageVerifier()
    doubao = TwoStageVerifier()
    doubao.id = "doubao"
    service = CollectionService(
        store,
        {"web_search": EmptyProvider()},
        {"deepseek": deepseek, "doubao": doubao},
    )
    task = service.create_task(_payload(["deepseek", "doubao"]))

    service.prepare_start(task["id"])
    service.run_task(task["id"])
    completed = service.get_task(task["id"])

    assert deepseek.discovery_calls == [("武汉市", ["软装公司", "整装公司"])]
    assert deepseek.enrichment_calls == []
    assert len(doubao.enrichment_calls) == 4
    assert completed["enrichment_completed"] == 4
    assert {lead["enrichment_model"] for lead in completed["leads"]} == {"doubao"}


def test_enriched_contact_is_persisted_before_erp_delivery(tmp_path: Path) -> None:
    store = RecordingStore(tmp_path)
    delivery = RecordingDelivery(store)
    deepseek = TwoStageVerifier()
    doubao = TwoStageVerifier()
    doubao.id = "doubao"
    service = CollectionService(
        store,
        {"web_search": EmptyProvider()},
        {"deepseek": deepseek, "doubao": doubao},
        lead_delivery=delivery,
    )
    task = service.create_task({**_payload(["deepseek", "doubao"]), "auto_deliver": True})

    service.prepare_start(task["id"])
    service.run_task(task["id"])

    assert len(delivery.delivered) == 3
    assert all(lead.get("mobile") for lead in delivery.delivered)


def test_auto_delivery_is_off_and_marks_leads_display_only(tmp_path: Path) -> None:
    store = RecordingStore(tmp_path)
    delivery = RecordingDelivery(store)
    doubao = TwoStageVerifier()
    doubao.id = "doubao"
    service = CollectionService(
        store,
        {"web_search": EmptyProvider()},
        {"deepseek": TwoStageVerifier(), "doubao": doubao},
        lead_delivery=delivery,
    )
    task = service.create_task(_payload(["deepseek"]))

    service.prepare_start(task["id"])
    service.run_task(task["id"])

    assert task["auto_deliver"] is False
    assert delivery.delivered == []
    assert {
        lead["delivery_status"] for lead in store.list_leads(task["id"])
    } == {"display_only"}


def test_verified_auto_delivery_only_sends_eligible_leads(tmp_path: Path) -> None:
    store = RecordingStore(tmp_path)
    delivery = RecordingDelivery(store)
    service = CollectionService(
        store,
        {"web_search": EmptyProvider()},
        lead_delivery=delivery,
    )
    task = service.create_task(
        {**_payload([]), "verified_auto_deliver": True}
    )
    eligible = {
        "city": "深圳市",
        "company_name": "深圳合格软装公司",
        "mobile": "13800138000",
        "email": None,
        "wechat": None,
        "source_url": "https://example.test/eligible",
        "contact_verification_status": "verified_official_page",
        "contact_evidence_url": "https://example.test/eligible",
        "verification_status": "single_model_match",
        "company_name_verification_status": "verified_official_page",
    }
    leads = [
        eligible,
        {**eligible, "company_name": "深圳缺手机号公司", "mobile": None},
        {
            **eligible,
            "company_name": "深圳缺来源公司",
            "contact_evidence_url": None,
        },
        {
            **eligible,
            "company_name": "深圳模型分歧公司",
            "contact_verification_status": "verified_cross_source",
            "company_name_verification_status": "verified_cross_source",
            "verification_status": "model_disagreement",
        },
    ]
    task["leads"] = leads
    store.save(task)

    for lead in leads:
        service._deliver_verified_lead(task, lead, check_after=False)

    assert [lead["company_name"] for lead in delivery.delivered] == [
        "深圳合格软装公司"
    ]
    persisted = {lead["company_name"]: lead for lead in store.list_leads(task["id"])}
    assert persisted["深圳合格软装公司"]["delivery_status"] == "succeeded"
    assert persisted["深圳缺手机号公司"]["delivery_error"] == "missing_mobile_or_email"
    assert persisted["深圳缺来源公司"]["delivery_error"] == "missing_source_url"
    assert (
        persisted["深圳模型分歧公司"]["delivery_error"]
        == "verification_not_matched"
    )


def test_verified_auto_delivery_never_resends_succeeded_lead(tmp_path: Path) -> None:
    store = RecordingStore(tmp_path)
    delivery = RecordingDelivery(store)
    service = CollectionService(
        store,
        {"web_search": EmptyProvider()},
        lead_delivery=delivery,
    )
    task = service.create_task(
        {**_payload([]), "verified_auto_deliver": True}
    )
    lead = {
        "city": "深圳市",
        "company_name": "深圳幂等软装公司",
        "mobile": "13800138000",
        "email": None,
        "wechat": None,
        "source_url": "https://example.test/idempotent",
        "contact_verification_status": "verified_cross_source",
        "contact_evidence_url": "https://example.test/idempotent",
        "verification_status": "model_consensus_match",
        "company_name_verification_status": "verified_cross_source",
    }
    task["leads"] = [lead]
    store.save(task)

    service._deliver_verified_lead(task, lead, check_after=False)
    service._deliver_verified_lead(task, lead, check_after=False)

    assert len(delivery.delivered) == 1
    assert lead["delivery_status"] == "succeeded"


def test_verified_delivery_retry_rechecks_current_eligibility(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(503)

    store = TaskStore(tmp_path)
    delivery = ErpLeadDelivery(
        tmp_path,
        "https://receiver.example.test/erp",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    service = CollectionService(
        store,
        {"web_search": EmptyProvider()},
        lead_delivery=delivery,
    )
    task = service.create_task(
        {**_payload([]), "verified_auto_deliver": True}
    )
    lead = {
        "city": "深圳市",
        "company_name": "深圳重试资格公司",
        "normalized_company_name": "深圳重试资格公司",
        "mobile": "13800138000",
        "email": None,
        "wechat": None,
        "source_url": "https://example.test/retry-gate",
        "contact_verification_status": "verified_cross_source",
        "contact_evidence_url": "https://example.test/retry-gate",
        "saved_at": "2026-07-28T10:00:00+08:00",
        "verification_status": "single_model_match",
        "company_name_verification_status": "verified_cross_source",
    }
    task["leads"] = [lead]
    store.save(task)
    service._deliver_verified_lead(task, lead, check_after=False)
    assert lead["delivery_status"] == "failed"

    lead["verification_status"] = "model_disagreement"
    store.save_leads(task["id"], [lead])
    result = service.retry_delivery(task["id"])

    assert result == {"attempted": 0, "succeeded": 0, "failed": 0}
    assert len(requests) == 1


def test_enabled_auto_delivery_persists_each_status(tmp_path: Path) -> None:
    store = RecordingStore(tmp_path)
    delivery = BlockingRecordingDelivery(store)
    payload = {**_payload(["deepseek"]), "auto_deliver": True, "limit_per_city": 2}
    doubao = TwoStageVerifier()
    doubao.id = "doubao"
    service = CollectionService(
        store,
        {"web_search": EmptyProvider()},
        {"deepseek": TwoStageVerifier(), "doubao": doubao},
        lead_delivery=delivery,
        max_concurrent_tasks=1,
    )
    task = service.create_task(payload)
    try:
        service.start_task(task["id"])
        assert delivery.wait_until_sending()
        sending = next(
            lead
            for lead in store.list_leads(task["id"])
            if lead.get("delivery_status") == "sending"
        )
        assert sending["delivery_error"] is None
        assert any(
            batch[0].get("delivery_status") == "pending"
            for batch in store.lead_batches
            if len(batch) == 1
        )

        delivery.release.set()
        assert _wait_for_task_completion(service, task["id"])
        succeeded = next(
            lead
            for lead in store.list_leads(task["id"])
            if lead.get("delivery_status") == "succeeded"
        )
        assert succeeded["delivery_error"] is None
        assert succeeded["delivered_at"]
    finally:
        delivery.release.set()
        service.shutdown()


def test_erp_failure_is_saved_without_stopping_later_leads(tmp_path: Path) -> None:
    store = RecordingStore(tmp_path)
    delivery = FixedResultDelivery(store, status="failed", error="IP not whitelisted")
    doubao = TwoStageVerifier()
    doubao.id = "doubao"
    service = CollectionService(
        store,
        {"web_search": EmptyProvider()},
        {"deepseek": TwoStageVerifier(), "doubao": doubao},
        lead_delivery=delivery,
    )
    task = service.create_task({**_payload(["deepseek"]), "auto_deliver": True})

    service.prepare_start(task["id"])
    service.run_task(task["id"])
    completed = service.get_task(task["id"])
    failed = [
        lead for lead in completed["leads"] if lead.get("delivery_status") == "failed"
    ]

    assert completed["status"] == "completed"
    assert len(delivery.delivered) == 3
    assert len(failed) == 3
    assert {lead["delivery_error"] for lead in failed} == {"IP not whitelisted"}
    assert all(lead["delivered_at"] is None for lead in failed)


def test_skipped_erp_fingerprint_is_a_successful_delivery(tmp_path: Path) -> None:
    store = RecordingStore(tmp_path)
    delivery = FixedResultDelivery(store, status="skipped")
    doubao = TwoStageVerifier()
    doubao.id = "doubao"
    service = CollectionService(
        store,
        {"web_search": EmptyProvider()},
        {"deepseek": TwoStageVerifier(), "doubao": doubao},
        lead_delivery=delivery,
    )
    task = service.create_task(
        {**_payload(["deepseek"]), "auto_deliver": True, "limit_per_city": 2}
    )

    service.prepare_start(task["id"])
    service.run_task(task["id"])

    delivered = next(
        lead for lead in store.list_leads(task["id"])
        if lead.get("delivery_status") == "succeeded"
    )
    assert delivered["delivered_at"]


def test_doubao_enrichment_failure_never_falls_back_to_deepseek(tmp_path: Path) -> None:
    store = RecordingStore(tmp_path)
    deepseek = TwoStageVerifier()
    service = CollectionService(
        store,
        {"web_search": EmptyProvider()},
        {"deepseek": deepseek, "doubao": FailingEnricher()},
    )
    task = service.create_task(_payload(["deepseek", "doubao"]))

    service.prepare_start(task["id"])
    service.run_task(task["id"])
    completed = service.get_task(task["id"])

    assert deepseek.enrichment_calls == []
    assert all(lead["enrichment_state"] == "failed" for lead in completed["leads"])
    assert all("doubao" in lead["enrichment_error"] for lead in completed["leads"])


def test_two_stage_contact_dedup_reconciles_each_city_persisted_survivors(
    tmp_path: Path,
) -> None:
    store = TaskStore(tmp_path)
    payload = _payload(["deepseek"])
    payload["cities"] = ["武汉市", "长沙市"]
    service = CollectionService(
        store,
        {"web_search": EmptyProvider()},
        {
            "deepseek": TwoStageVerifier(),
            "doubao": DuplicateContactEnricher(),
        },
    )
    task = service.create_task(payload)

    service.prepare_start(task["id"])
    service.run_task(task["id"])

    completed = service.get_task(task["id"])
    persisted = store.list_leads(task["id"])
    assert completed["lead_count"] == 2
    assert len(completed["leads"]) == 2
    assert len(persisted) == 2
    assert {lead["city"] for lead in persisted} == {"武汉市", "长沙市"}
    assert store.count_leads(task["id"], city="武汉市") == 1
    assert store.count_leads(task["id"], city="长沙市") == 1


def test_legacy_collection_and_verification_persist_changed_lead_batches(
    tmp_path: Path,
) -> None:
    store = RecordingStore(tmp_path)
    service = CollectionService(
        store,
        {"web_search": LegacyProvider()},
        {"deepseek": LegacyVerifier()},
    )
    task = service.create_task(_payload(["deepseek"]))

    service.prepare_start(task["id"])
    service.run_task(task["id"])

    assert len(store.reconciliations) == 2
    assert all(city == "武汉市" for city, _ in store.reconciliations)
    assert [len(leads) for _, leads in store.reconciliations] == [1, 2]

    store.lead_batches.clear()
    service.prepare_verification(task["id"])
    service.verify_task(task["id"])

    assert len(store.lead_batches) == 2
    assert all(batch[0]["verification_status"] == "single_model_match" for batch in store.lead_batches)


def test_legacy_collection_skips_delivered_and_reserved_companies(
    tmp_path: Path,
) -> None:
    delivery = ErpLeadDelivery(tmp_path, target_url="https://erp.example.test/leads")
    delivered = delivery.dedup_ledger.evaluate(
        city="Shenzhen",
        company_name="Delivered Design Co",
        mobile="13800138000",
        evidence_score=90,
    )
    delivery.dedup_ledger.mark_delivered(delivered)
    service = CollectionService(
        TaskStore(tmp_path),
        {"web_search": EmptyProvider()},
        lead_delivery=delivery,
    )
    delivered_task = service.create_task(_payload(["deepseek"]))
    first = service.create_task(_payload(["deepseek"]))
    second = service.create_task(_payload(["deepseek"]))
    delivered_candidate = normalize_lead(
        {"company_name": "Delivered Design Co", "mobile": "13800138000"},
        city="Shenzhen",
        provider="deepseek",
    )
    reserved_candidate = normalize_lead(
        {"company_name": "Reserved Design Co", "mobile": "13800138001"},
        city="Shenzhen",
        provider="deepseek",
    )

    try:
        service._persist_legacy_candidates(
            delivered_task, "Shenzhen", [delivered_candidate]
        )
        service._persist_legacy_candidates(first, "Shenzhen", [reserved_candidate])
        service._persist_legacy_candidates(second, "Shenzhen", [reserved_candidate])

        assert service.get_task(delivered_task["id"])["leads"] == []
        assert len(service.get_task(first["id"])["leads"]) == 1
        assert service.get_task(second["id"])["leads"] == []
    finally:
        service.shutdown()


def test_legacy_collection_skips_companies_from_completed_history(
    tmp_path: Path,
) -> None:
    service = CollectionService(
        TaskStore(tmp_path),
        {"web_search": EmptyProvider()},
    )
    history = service.create_task(_payload(["deepseek"]))
    history_lead = normalize_lead(
        {"company_name": "Completed Design Co", "mobile": "13800138002"},
        city="深圳市",
        provider="deepseek",
    )
    history["leads"] = [history_lead]
    history["lead_count"] = 1
    history["status"] = "completed"
    service.store.save(history)
    current = service.create_task(_payload(["deepseek"]))

    try:
        service._persist_legacy_candidates(current, "深圳市", [history_lead])

        assert service.get_task(current["id"])["leads"] == []
    finally:
        service.shutdown()


def test_continuous_pool_prunes_companies_already_seen_in_history(
    tmp_path: Path,
) -> None:
    service = CollectionService(
        TaskStore(tmp_path),
        {"web_search": EmptyProvider()},
    )
    history = service.create_task(_payload(["deepseek"]))
    history_lead = normalize_lead(
        {"company_name": "Previously Seen Design Co", "mobile": "13800138003"},
        city="深圳市",
        provider="deepseek",
    )
    history.update(status="completed", leads=[history_lead], lead_count=1)
    service.store.save(history)
    current = service.create_task(_payload(["deepseek"]))
    current["leads"] = [
        history_lead,
        {**history_lead, "company_name": "Fresh Design Co", "mobile": "13800138004"},
    ]
    current["lead_count"] = 2
    service.store.save(current)

    try:
        service._prune_historical_candidate_duplicates(current)

        assert [lead["company_name"] for lead in current["leads"]] == ["Fresh Design Co"]
    finally:
        service.shutdown()


def test_resume_after_first_enrichment_checkpoint_does_not_repeat_discovery(
    tmp_path: Path,
) -> None:
    deepseek = TwoStageVerifier()
    doubao = TwoStageVerifier()
    doubao.id = "doubao"
    service = PauseBeforeFirstEnrichmentService(
        TaskStore(tmp_path),
        {"web_search": EmptyProvider()},
        {"deepseek": deepseek, "doubao": doubao},
        max_concurrent_tasks=1,
    )
    task_id = service.create_task(_payload(["deepseek"]))["id"]
    try:
        service.start_task(task_id)
        deadline = monotonic() + 2
        while monotonic() < deadline:
            if service.get_task(task_id)["status"] == "paused":
                break
            sleep(0.01)
        assert service.get_task(task_id)["status"] == "paused"
        assert len(deepseek.discovery_calls) == 1

        service.resume_task(task_id)
        deadline = monotonic() + 3
        while monotonic() < deadline:
            if service.get_task(task_id)["status"] in {
                "completed",
                "completed_with_errors",
            }:
                break
            sleep(0.01)

        assert service.get_task(task_id)["status"] == "completed"
        assert len(deepseek.discovery_calls) == 1
    finally:
        service.shutdown()


def test_pause_drains_in_flight_enrichment_results_before_resume(
    tmp_path: Path,
) -> None:
    payload = _payload(["deepseek"])
    payload["limit_per_city"] = 3
    payload["auto_deliver"] = True
    enricher = BlockingInFlightEnricher()
    store = TaskStore(tmp_path)
    delivery = RecordingDelivery(store)
    service = CollectionService(
        store,
        {"web_search": EmptyProvider()},
        {"deepseek": TwoStageVerifier(), "doubao": enricher},
        lead_delivery=delivery,
        max_concurrent_tasks=1,
    )
    task_id = service.create_task(payload)["id"]
    try:
        service.start_task(task_id)
        assert enricher.wait_for_calls(3)
        service.pause_task(task_id)
        enricher.release.set()

        deadline = monotonic() + 2
        while monotonic() < deadline:
            if service.get_task(task_id)["status"] == "paused":
                break
            sleep(0.01)
        assert service.get_task(task_id)["status"] == "paused"

        service.resume_task(task_id)
        deadline = monotonic() + 3
        while monotonic() < deadline:
            if service.get_task(task_id)["status"] in {
                "completed",
                "completed_with_errors",
            }:
                break
            sleep(0.01)

        assert service.get_task(task_id)["status"] == "completed"
        assert set(enricher.call_counts.values()) == {1}
        delivered_names = [lead["company_name"] for lead in delivery.delivered]
        assert len(delivered_names) == 3
        assert len(set(delivered_names)) == 3
    finally:
        enricher.release.set()
        service.shutdown()


def test_pause_at_pending_delivery_resumes_without_repeating_models(
    tmp_path: Path,
) -> None:
    payload = {
        **_payload(["deepseek"]),
        "auto_deliver": True,
        "limit_per_city": 2,
    }
    store = PendingBlockingStore(tmp_path)
    deepseek = TwoStageVerifier()
    doubao = TwoStageVerifier()
    doubao.id = "doubao"
    delivery = RecordingDelivery(store)
    service = CollectionService(
        store,
        {"web_search": EmptyProvider()},
        {"deepseek": deepseek, "doubao": doubao},
        lead_delivery=delivery,
        max_concurrent_tasks=1,
    )
    task_id = service.create_task(payload)["id"]
    try:
        service.start_task(task_id)
        assert store.pending.wait(timeout=2)
        service.pause_task(task_id)
        store.release_pending.set()
        deadline = monotonic() + 2
        while monotonic() < deadline and service.get_task(task_id)["status"] != "paused":
            sleep(0.01)

        assert service.get_task(task_id)["status"] == "paused"
        assert any(
            lead.get("delivery_status") == "pending"
            for lead in store.list_leads(task_id)
        )

        service.resume_task(task_id)
        assert _wait_for_task_completion(service, task_id)
        assert len(deepseek.discovery_calls) == 1
        assert len(doubao.enrichment_calls) == 2
        assert len(delivery.delivered) == 1
        assert any(
            lead.get("delivery_status") == "succeeded"
            for lead in store.list_leads(task_id)
        )
    finally:
        store.release_pending.set()
        service.shutdown()


def test_service_startup_recovers_sending_delivery_to_pending(tmp_path: Path) -> None:
    store = TaskStore(tmp_path)
    first = CollectionService(store, {"web_search": EmptyProvider()})
    task = first.create_task(_payload([]))
    first.shutdown()
    stored = store.load(task["id"])
    stored["status"] = "paused"
    stored["leads"] = [
        {
            "city": "武汉市",
            "company_name": "武汉重启恢复公司",
            "normalized_company_name": "武汉重启恢复公司",
            "mobile": "13800138000",
            "enrichment_state": "completed",
            "delivery_status": "sending",
            "delivery_error": None,
            "delivered_at": None,
        }
    ]
    store.save(stored)

    recovered = CollectionService(store, {"web_search": EmptyProvider()})
    try:
        lead = recovered.get_task(task["id"])["leads"][0]
        assert lead["delivery_status"] == "pending"
        assert lead["delivery_error"] is None
    finally:
        recovered.shutdown()


def test_task_store_recovers_stale_sending_delivery_to_pending(tmp_path: Path) -> None:
    store = TaskStore(tmp_path)
    service = CollectionService(store, {"web_search": EmptyProvider()})
    task = service.create_task(_payload([]))
    stored = store.load(task["id"])
    stored["leads"] = [
        {
            "city": "武汉市",
            "company_name": "武汉超时推送公司",
            "normalized_company_name": "武汉超时推送公司",
            "mobile": "13800138000",
            "delivery_status": "sending",
            "delivery_started_at": (datetime.now(UTC) - timedelta(minutes=10)).isoformat(),
            "delivery_error": None,
            "delivered_at": None,
        }
    ]
    stored["lead_count"] = 1
    store.save(stored)

    try:
        recovered = store.load(task["id"])["leads"][0]
        assert recovered["delivery_status"] == "pending"
        assert recovered["delivery_started_at"] is None
        assert recovered["delivery_error"] is None
    finally:
        service.shutdown()


def test_task_store_keeps_recent_sending_delivery_claim(tmp_path: Path) -> None:
    store = TaskStore(tmp_path)
    service = CollectionService(store, {"web_search": EmptyProvider()})
    task = service.create_task(_payload([]))
    stored = store.load(task["id"])
    stored["leads"] = [
        {
            "city": "武汉市",
            "company_name": "武汉进行中推送公司",
            "normalized_company_name": "武汉进行中推送公司",
            "mobile": "13800138000",
            "delivery_status": "sending",
            "delivery_started_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
        }
    ]
    stored["lead_count"] = 1
    store.save(stored)

    try:
        assert store.load(task["id"])["leads"][0]["delivery_status"] == "sending"
    finally:
        service.shutdown()


def test_legacy_lead_without_delivery_fields_is_display_only(tmp_path: Path) -> None:
    store = TaskStore(tmp_path)
    service = CollectionService(store, {"web_search": EmptyProvider()})
    task = service.create_task(_payload([]))
    stored = store.load(task["id"])
    stored["leads"] = [
        {
            "city": "武汉市",
            "company_name": "武汉历史线索公司",
            "normalized_company_name": "武汉历史线索公司",
            "mobile": "13800138000",
        }
    ]
    store.save(stored)
    try:
        lead = store.list_leads(task["id"])[0]
        assert lead["delivery_status"] == "display_only"
        assert lead["delivery_error"] is None
        assert lead["delivered_at"] is None
    finally:
        service.shutdown()


@pytest.mark.parametrize(
    "first_response",
    [
        httpx.Response(200, json={"errCode": 0, "errMsg": None}),
        httpx.Response(200, json={"errCode": 49302, "errMsg": "temporary failure"}),
    ],
    ids=["succeeded", "failed"],
)
def test_changed_contact_reenters_delivery_after_terminal_status(
    tmp_path: Path,
    first_response: httpx.Response,
) -> None:
    requests: list[httpx.Request] = []
    responses = iter(
        [
            first_response,
            httpx.Response(200, json={"errCode": 0, "errMsg": None}),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return next(responses)

    store = TaskStore(tmp_path)
    delivery = ErpLeadDelivery(
        tmp_path,
        "https://receiver.example.test/erp",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    service = CollectionService(
        store,
        {"web_search": EmptyProvider()},
        lead_delivery=delivery,
    )
    task = service.create_task({**_payload([]), "auto_deliver": True})
    lead = {
        "city": "武汉市",
        "company_name": "武汉联系方式变化公司",
        "normalized_company_name": "武汉联系方式变化公司",
        "mobile": "13800138000",
        "email": None,
        "wechat": None,
        "contact_evidence_score": 80,
        "saved_at": "2026-07-28T10:00:00+08:00",
    }
    task["leads"] = [lead]
    store.save(task)
    try:
        service._deliver_lead(task, lead, check_after=False)
        first_fingerprint = lead.get("delivery_fingerprint")

        lead["mobile"] = "13900139000"
        lead["contact_evidence_score"] = 95
        store.save_leads(task["id"], [lead])
        service._deliver_lead(task, lead, check_after=False)

        assert len(requests) == 2
        assert lead["delivery_status"] == "succeeded"
        assert lead["delivery_error"] is None
        assert lead["delivery_fingerprint"] != first_fingerprint
    finally:
        service.shutdown()


def test_verify_and_retry_cannot_overwrite_newer_delivery_status(tmp_path: Path) -> None:
    responses = iter(
        [
            httpx.Response(503),
            httpx.Response(200, json={"errCode": 0, "errMsg": None}),
        ]
    )
    delivery = ErpLeadDelivery(
        tmp_path,
        "https://receiver.example.test/erp",
        client=httpx.Client(
            transport=httpx.MockTransport(lambda request: next(responses))
        ),
    )
    verifier = BlockingLegacyVerifier()
    store = TaskStore(tmp_path)
    service = CollectionService(
        store,
        {"web_search": EmptyProvider()},
        {"deepseek": verifier},
        lead_delivery=delivery,
    )
    task = service.create_task({**_payload(["deepseek"]), "auto_deliver": True})
    lead = {
        "city": "武汉市",
        "company_name": "武汉并发状态公司",
        "normalized_company_name": "武汉并发状态公司",
        "mobile": "13800138000",
        "email": None,
        "wechat": None,
        "saved_at": "2026-07-28T10:00:00+08:00",
        "matched_keyword": "软装公司",
        "discovery_channel": "web_search",
        "verification_status": "unverified",
    }
    failed = delivery.deliver(task, lead)
    assert failed.status == "failed"
    lead.update(
        delivery_status="failed",
        delivery_error=failed.error,
        delivered_at=None,
        delivery_fingerprint=failed.fingerprint,
    )
    task.update(status="completed", leads=[lead], lead_count=1)
    store.save(task)
    service.prepare_verification(task["id"])
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            verify_future = executor.submit(service.verify_task, task["id"])
            assert verifier.started.wait(timeout=2)
            retry_future = executor.submit(service.retry_delivery, task["id"])
            sleep(0.05)
            verifier.release.set()
            verify_future.result(timeout=2)
            retry_result = retry_future.result(timeout=2)

        assert retry_result == {"attempted": 1, "succeeded": 1, "failed": 0}
        persisted = store.list_leads(task["id"])[0]
        assert persisted["delivery_status"] == "succeeded"
        assert persisted["delivery_error"] is None
        assert persisted["delivered_at"]
    finally:
        verifier.release.set()
        service.shutdown()


def test_prepare_verification_waits_for_running_delivery_operation(
    tmp_path: Path,
) -> None:
    store = RecordingStore(tmp_path)
    delivery = BlockingRecordingDelivery(store)
    deepseek = VerifyingTwoStageVerifier()
    doubao = TwoStageVerifier()
    doubao.id = "doubao"
    service = CollectionService(
        store,
        {"web_search": EmptyProvider()},
        {"deepseek": deepseek, "doubao": doubao},
        lead_delivery=delivery,
        max_concurrent_tasks=1,
    )
    task = service.create_task(
        {
            **_payload(["deepseek"]),
            "auto_deliver": True,
            "limit_per_city": 2,
        }
    )
    prepare_started = Event()

    def prepare() -> tuple[dict[str, Any], bool]:
        prepare_started.set()
        return service.prepare_verification(task["id"])

    try:
        service.start_task(task["id"])
        assert delivery.wait_until_sending()
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(prepare)
            assert prepare_started.wait(timeout=2)
            with pytest.raises(FutureTimeoutError):
                future.result(timeout=0.05)

            delivery.release.set()
            assert _wait_for_task_completion(service, task["id"])
            prepared, should_run = future.result(timeout=2)

        assert should_run is True
        assert prepared["verification_state"] == "running"
        assert any(
            lead["delivery_status"] == "succeeded"
            for lead in store.list_leads(task["id"])
        )

        service.verify_task(task["id"])
        verified = service.get_task(task["id"])
        assert verified["verification_state"] == "completed"
        assert any(
            lead["delivery_status"] == "succeeded"
            for lead in verified["leads"]
        )
    finally:
        delivery.release.set()
        service.shutdown()
