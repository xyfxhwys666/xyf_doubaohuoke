from pathlib import Path
from threading import Condition, Event, Lock
from time import monotonic, sleep
from typing import Any

from app.collection.doubao_web import DoubaoWebResponseError
from app.collection.scheduler import TaskScheduler
from app.collection.service import CollectionService, TaskStore


def _payload(name: str, *, keywords: list[str] | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "request_text": name,
        "cities": ["武汉市"],
        "all_cities": False,
        "keywords": keywords or ["软装公司"],
        "providers": ["web_search"],
        "models": [],
        "required_contacts": ["mobile"],
        "contact_match": "any",
        "limit_per_city": None,
    }


def _wait_for_status(
    service: CollectionService,
    task_id: str,
    status: str,
    *,
    timeout: float = 2.0,
) -> bool:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if service.get_task(task_id)["status"] == status:
            return True
        sleep(0.01)
    return False


class BlockingProvider:
    id = "web_search"
    label = "公开网页"
    configured = True

    def __init__(self) -> None:
        self._condition = Condition()
        self._started: list[str] = []
        self._released: set[str] = set()

    def collect(
        self,
        *,
        city: str,
        keyword: str,
        request_text: str,
    ) -> list[dict[str, Any]]:
        with self._condition:
            self._started.append(request_text)
            self._condition.notify_all()
            self._condition.wait_for(lambda: request_text in self._released, timeout=3)
        return [
            {
                "company_name": f"{request_text}公司",
                "mobile": "13800138000",
                "source_url": "https://example.test/lead",
            }
        ]

    def wait_until_started(self, count: int, *, timeout: float = 2.0) -> bool:
        with self._condition:
            return self._condition.wait_for(
                lambda: len(self._started) >= count,
                timeout=timeout,
            )

    def started(self, name: str) -> bool:
        with self._condition:
            return name in self._started

    def release(self, name: str) -> None:
        with self._condition:
            self._released.add(name)
            self._condition.notify_all()

    def release_all(self) -> None:
        with self._condition:
            self._released.update(self._started)
            self._condition.notify_all()


class CheckpointProvider:
    id = "web_search"
    label = "公开网页"
    configured = True

    def __init__(self) -> None:
        self.started = {"第一阶段": Event(), "第二阶段": Event()}
        self.released = {"第一阶段": Event(), "第二阶段": Event()}
        self.calls: list[str] = []
        self._lock = Lock()

    def collect(
        self,
        *,
        city: str,
        keyword: str,
        request_text: str,
    ) -> list[dict[str, Any]]:
        with self._lock:
            self.calls.append(keyword)
        self.started[keyword].set()
        self.released[keyword].wait(timeout=3)
        return [
            {
                "company_name": f"{keyword}公司",
                "mobile": "13800138000" if keyword == "第一阶段" else "13800138001",
                "source_url": f"https://example.test/{keyword}",
            }
        ]


class EmptyProvider:
    id = "web_search"
    label = "公开网页"
    configured = True

    def collect(
        self,
        *,
        city: str,
        keyword: str,
        request_text: str,
    ) -> list[dict[str, Any]]:
        return []


class WorkingProvider:
    id = "web_search"
    label = "公开网页"
    configured = True

    def collect(
        self,
        *,
        city: str,
        keyword: str,
        request_text: str,
    ) -> list[dict[str, Any]]:
        return [{
            "company_name": f"{request_text}公司",
            "mobile": "13800138000",
            "source_url": "https://example.test/lead",
        }]


class ExplodingProvider(WorkingProvider):
    def collect(
        self,
        *,
        city: str,
        keyword: str,
        request_text: str,
    ) -> list[dict[str, Any]]:
        error_type = type("UnexpectedProviderFailure" + "X" * 200, (RuntimeError,), {})
        raise error_type("secret-token-must-not-be-persisted")


class BlockingDiscoveryVerifier:
    id = "deepseek"
    label = "DeepSeek"
    configured = True

    def __init__(self, *, candidate_count: int = 1) -> None:
        self.candidate_count = candidate_count
        self.started = Event()
        self.released = Event()

    def discover_companies(
        self,
        *,
        request_text: str,
        city: str,
        keywords: list[str],
        limit: int,
    ) -> list[dict[str, Any]]:
        self.started.set()
        self.released.wait(timeout=3)
        return [
            {
                "company_name": f"候选公司-{index}",
                "matched_keyword": keywords[0],
                "reason": "符合客户类型",
            }
            for index in range(self.candidate_count)
        ]


class BlockingEnricher:
    id = "doubao"
    label = "豆包"
    configured = True

    def __init__(self) -> None:
        self._condition = Condition()
        self.calls: list[str] = []
        self.release = Event()

    def enrich_company(
        self,
        *,
        request_text: str,
        city: str,
        company: dict[str, Any],
        required_contacts: list[str],
    ) -> dict[str, Any]:
        with self._condition:
            self.calls.append(company["company_name"])
            self._condition.notify_all()
        self.release.wait(timeout=3)
        return {
            **company,
            "mobile": "13800138000",
            "source_url": "https://example.test/enriched",
        }

    def wait_for_calls(self, count: int, *, timeout: float = 2.0) -> bool:
        with self._condition:
            return self._condition.wait_for(
                lambda: len(self.calls) >= count,
                timeout=timeout,
            )


def _stored_task(task_id: str, *, created_at: str = "2026-07-28T00:00:00+00:00") -> dict:
    return {
        "id": task_id,
        "status": "draft",
        "created_at": created_at,
        "leads": [],
    }


def test_list_tasks_returns_newest_first_and_backfills_lifecycle_defaults(tmp_path: Path) -> None:
    store = TaskStore(tmp_path)
    first = _stored_task("first", created_at="2026-07-28T01:00:00+00:00")
    second = _stored_task("second", created_at="2026-07-28T02:00:00+00:00")
    store.save(first)
    store.save(second)

    tasks = store.list_tasks()

    assert [task["id"] for task in tasks] == ["second", "first"]
    assert tasks[0]["auto_deliver"] is False
    assert tasks[0]["continuous_mode"] is False
    assert tasks[0]["verified_auto_deliver"] is False
    assert tasks[0]["round_number"] == 1
    assert tasks[0]["round_phase"] is None
    assert tasks[0]["round_metrics"]["candidates"] == 0
    assert tasks[0]["round_checkpoint"] == {}
    assert tasks[0]["pause_requested"] is False
    assert tasks[0]["terminate_requested"] is False


def test_round_defaults_are_not_shared_between_tasks(tmp_path: Path) -> None:
    store = TaskStore(tmp_path)
    store.save(_stored_task("first"))
    store.save(_stored_task("second"))

    first = store.load("first")
    first["round_metrics"]["candidates"] = 12
    first["round_checkpoint"]["city"] = "深圳市"

    second = store.load("second")

    assert second["round_metrics"]["candidates"] == 0
    assert second["round_checkpoint"] == {}


def test_update_mutates_one_task_without_losing_existing_leads(tmp_path: Path) -> None:
    store = TaskStore(tmp_path)
    task = _stored_task("task-1")
    task["leads"] = [{"city": "武汉市", "company_name": "甲公司"}]
    store.save(task)

    updated = store.update("task-1", lambda current: current.update(status="paused"))

    assert updated["status"] == "paused"
    assert updated["leads"] == [
        {
            **task["leads"][0],
            "delivery_status": "display_only",
            "delivery_error": None,
            "delivered_at": None,
            "delivery_fingerprint": None,
            "delivery_started_at": None,
            "company_name_verification_status": "unverified",
            "company_name_evidence_urls": [],
                "company_name_verification_reason": "",
                "company_name_verified_at": None,
                "company_name_verification_attempts": 0,
                "contact_person_name": None,
                "contact_person_role": None,
                "contact_verification_status": "unverified",
                "contact_evidence_url": None,
                "contact_evidence_quote": "",
                "contact_evidence_urls": [],
                "contact_evidence_quotes": [],
                "contact_evidence_grade": "C",
                "contact_evidence_score": 0,
                "contact_verified_at": None,
                "contact_verification_reason": "",
            }
    ]


def test_task_summary_separates_raw_contacts_from_strictly_verified_contacts() -> None:
    summary = CollectionService.task_summary(
        {
            "status": "draft",
            "cities": [],
            "leads": [
                {
                    "mobile": "13800138000",
                    "company_name_verification_status": "verified_public_web",
                    "contact_verification_status": "verified_public_web",
                    "contact_evidence_url": "https://example.test/company",
                    "verification_status": "single_model_match",
                },
                {"email": "sales@example.test"},
                {"wechat": None, "mobile": None, "email": None},
            ],
        }
    )

    assert summary["raw_contact_lead_count"] == 2
    assert summary["contact_lead_count"] == 1


def test_scheduler_runs_two_tasks_and_keeps_third_queued(tmp_path: Path) -> None:
    provider = BlockingProvider()
    service = CollectionService(
        TaskStore(tmp_path),
        {"web_search": provider},
        max_concurrent_tasks=2,
    )
    task_ids: list[str] = []
    try:
        for index in range(3):
            task = service.create_task(_payload(f"task-{index}"))
            task_ids.append(task["id"])
            service.start_task(task["id"])

        assert provider.wait_until_started(2)
        assert service.get_task(task_ids[0])["status"] == "running"
        assert service.get_task(task_ids[1])["status"] == "running"
        assert service.get_task(task_ids[2])["status"] == "queued"

        provider.release("task-0")
        assert provider.wait_until_started(3)
        assert provider.started("task-2")
    finally:
        provider.release_all()
        service.shutdown()


def test_pause_resume_and_terminate_are_cooperative_and_persistent(
    tmp_path: Path,
) -> None:
    provider = CheckpointProvider()
    service = CollectionService(
        TaskStore(tmp_path),
        {"web_search": provider},
        max_concurrent_tasks=1,
    )
    task = service.create_task(
        _payload("controlled-task", keywords=["第一阶段", "第二阶段"])
    )
    task_id = task["id"]
    try:
        service.start_task(task_id)
        assert provider.started["第一阶段"].wait(timeout=2)

        pausing = service.pause_task(task_id)
        assert pausing["status"] == "pausing"
        provider.released["第一阶段"].set()
        assert _wait_for_status(service, task_id, "paused")
        assert service.get_task(task_id)["leads"]

        resumed = service.resume_task(task_id)
        assert resumed["status"] in {"queued", "running"}
        assert provider.started["第二阶段"].wait(timeout=2)
        terminating = service.terminate_task(task_id)
        assert terminating["status"] in {"terminating", "terminated"}
        provider.released["第二阶段"].set()
        assert _wait_for_status(service, task_id, "terminated")
        assert service.get_task(task_id)["leads"]
    finally:
        provider.released["第一阶段"].set()
        provider.released["第二阶段"].set()
        service.shutdown()


def test_terminated_executor_queue_entry_never_starts_provider(tmp_path: Path) -> None:
    provider = BlockingProvider()
    service = CollectionService(
        TaskStore(tmp_path),
        {"web_search": provider},
        max_concurrent_tasks=2,
    )
    task_ids: list[str] = []
    try:
        for index in range(3):
            task = service.create_task(_payload(f"queued-{index}"))
            task_ids.append(task["id"])
            service.start_task(task["id"])
        assert provider.wait_until_started(2)

        terminated = service.terminate_task(task_ids[2])
        assert terminated["status"] == "terminated"
        provider.release("queued-0")
        assert _wait_for_status(service, task_ids[0], "completed")
        sleep(0.05)

        assert service.get_task(task_ids[2])["status"] == "terminated"
        assert not provider.started("queued-2")
    finally:
        provider.release_all()
        service.shutdown()


def test_pause_after_model_discovery_persists_candidates_before_stopping(
    tmp_path: Path,
) -> None:
    discovery = BlockingDiscoveryVerifier()
    enricher = BlockingEnricher()
    service = CollectionService(
        TaskStore(tmp_path),
        {"web_search": EmptyProvider()},
        {"deepseek": discovery, "doubao": enricher},
        max_concurrent_tasks=1,
    )
    payload = _payload("model-checkpoint")
    payload["models"] = ["deepseek"]
    task_id = service.create_task(payload)["id"]
    try:
        service.start_task(task_id)
        assert discovery.started.wait(timeout=2)
        service.pause_task(task_id)
        discovery.released.set()

        assert _wait_for_status(service, task_id, "paused")
        assert service.get_task(task_id)["leads"]
        assert enricher.calls == []
    finally:
        discovery.released.set()
        enricher.release.set()
        service.shutdown()


def test_pending_enrichment_future_checks_control_before_model_call(
    tmp_path: Path,
) -> None:
    discovery = BlockingDiscoveryVerifier(candidate_count=4)
    discovery.released.set()
    enricher = BlockingEnricher()
    service = CollectionService(
        TaskStore(tmp_path),
        {"web_search": EmptyProvider()},
        {"deepseek": discovery, "doubao": enricher},
        max_concurrent_tasks=1,
    )
    payload = _payload("enrichment-checkpoint")
    payload["models"] = ["deepseek"]
    task_id = service.create_task(payload)["id"]
    try:
        service.start_task(task_id)
        assert enricher.wait_for_calls(3)
        service.pause_task(task_id)
        enricher.release.set()

        assert _wait_for_status(service, task_id, "paused")
        assert len(enricher.calls) == 3
    finally:
        discovery.released.set()
        enricher.release.set()
        service.shutdown()


def test_running_task_is_requeued_after_scheduler_is_initialized(tmp_path: Path) -> None:
    store = TaskStore(tmp_path)
    provider = BlockingProvider()
    seed = CollectionService(
        store,
        {"web_search": provider},
        max_concurrent_tasks=1,
    )
    task = seed.create_task(_payload("recovered-task"))
    seed.shutdown()
    store.update(task["id"], lambda current: current.update(status="running"))

    recovered = CollectionService(
        store,
        {"web_search": provider},
        max_concurrent_tasks=1,
    )
    try:
        assert provider.wait_until_started(1)
        assert recovered.get_task(task["id"])["status"] == "running"
    finally:
        provider.release("recovered-task")
        recovered.shutdown()


def test_recovery_settles_interrupted_control_transitions(tmp_path: Path) -> None:
    store = TaskStore(tmp_path)
    seed = CollectionService(store, {"web_search": WorkingProvider()})
    pausing = seed.create_task(_payload("recovered-pausing"))
    terminating = seed.create_task(_payload("recovered-terminating"))
    seed.shutdown()
    store.update(
        pausing["id"],
        lambda current: current.update(status="pausing", pause_requested=True),
    )
    store.update(
        terminating["id"],
        lambda current: current.update(status="terminating", terminate_requested=True),
    )

    recovered = CollectionService(store, {"web_search": WorkingProvider()})
    try:
        assert recovered.get_task(pausing["id"])["status"] == "paused"
        assert recovered.get_task(terminating["id"])["status"] == "terminated"
    finally:
        recovered.shutdown()


def test_recovery_submits_interrupted_tasks_oldest_first(tmp_path: Path) -> None:
    store = TaskStore(tmp_path)
    provider = BlockingProvider()
    seed = CollectionService(
        store,
        {"web_search": provider},
        max_concurrent_tasks=1,
    )
    newest = seed.create_task(_payload("newest-recovered"))
    oldest = seed.create_task(_payload("oldest-recovered"))
    seed.shutdown()
    store.update(
        newest["id"],
        lambda current: current.update(
            status="running",
            created_at="2026-07-28T02:00:00+00:00",
        ),
    )
    store.update(
        oldest["id"],
        lambda current: current.update(
            status="running",
            created_at="2026-07-28T01:00:00+00:00",
        ),
    )

    recovered = CollectionService(
        store,
        {"web_search": provider},
        max_concurrent_tasks=1,
    )
    try:
        assert provider.wait_until_started(1)
        assert provider.started("oldest-recovered")
        assert not provider.started("newest-recovered")
    finally:
        provider.release("oldest-recovered")
        provider.release("newest-recovered")
        recovered.shutdown()


def test_resume_running_task_requeues_when_worker_is_missing(tmp_path: Path) -> None:
    """A persisted running task can be safely reattached after a restart."""
    store = TaskStore(tmp_path)
    provider = BlockingProvider()
    service = CollectionService(
        store,
        {"web_search": provider},
        max_concurrent_tasks=1,
    )
    task = service.create_task(_payload("resume-orphaned-task"))
    store.update(task["id"], lambda current: current.update(status="running"))

    try:
        resumed = service.resume_task(task["id"])

        assert resumed["status"] == "running"
        assert provider.wait_until_started(1)
    finally:
        provider.release("resume-orphaned-task")
        service.shutdown()


def test_resume_running_task_does_not_duplicate_an_active_worker(tmp_path: Path) -> None:
    store = TaskStore(tmp_path)
    provider = BlockingProvider()
    service = CollectionService(
        store,
        {"web_search": provider},
        max_concurrent_tasks=1,
    )
    task = service.create_task(_payload("resume-active-task"))

    try:
        service.start_task(task["id"])
        assert provider.wait_until_started(1)

        resumed = service.resume_task(task["id"])
        assert resumed["status"] == "running"

        provider.release("resume-active-task")
        assert _wait_for_status(service, task["id"], "completed")
        assert not provider.wait_until_started(2, timeout=0.1)
    finally:
        provider.release("resume-active-task")
        service.shutdown()


def test_shutdown_returns_without_waiting_for_an_active_request(tmp_path: Path) -> None:
    provider = BlockingProvider()
    service = CollectionService(
        TaskStore(tmp_path),
        {"web_search": provider},
        max_concurrent_tasks=1,
    )
    task_id = service.create_task(_payload("shutdown-task"))["id"]
    service.start_task(task_id)
    assert provider.wait_until_started(1)

    started_at = monotonic()
    service.shutdown()
    elapsed = monotonic() - started_at
    provider.release("shutdown-task")

    assert elapsed < 0.2


def test_cancel_resubmit_blocks_callback_then_clears_scheduler_tombstone() -> None:
    started = Event()
    released = Event()
    finished = Event()
    calls: list[str] = []

    def runner(task_id: str) -> None:
        calls.append(task_id)
        started.set()
        released.wait(timeout=2)
        finished.set()

    scheduler = TaskScheduler(runner, max_workers=1)
    try:
        assert scheduler.submit("terminated-task")
        assert started.wait(timeout=2)
        assert not scheduler.submit(
            "terminated-task",
            resubmit_if_running=True,
        )

        scheduler.cancel_resubmit("terminated-task")
        released.set()
        assert finished.wait(timeout=2)
        sleep(0.05)

        assert calls == ["terminated-task"]
        assert scheduler.submit("terminated-task")
        deadline = monotonic() + 2
        while monotonic() < deadline and len(calls) < 2:
            sleep(0.01)
        assert calls == ["terminated-task", "terminated-task"]
    finally:
        released.set()
        scheduler.shutdown()


def test_retry_errors_resubmits_after_a_stale_future_finishes(tmp_path: Path) -> None:
    service = CollectionService(TaskStore(tmp_path), {"web_search": WorkingProvider()})
    task_id = service.create_task(_payload("retry-after-stale-future"))["id"]
    service.store.update(
        task_id,
        lambda task: task.update(
            status="completed_with_errors",
            unit_errors=1,
            activity=[
                {
                    "action": "collect_unit",
                    "city": "武汉市",
                    "keyword": "软装公司",
                    "provider": "web_search",
                    "outcome": "error",
                }
            ],
        ),
    )
    stale_started = Event()
    release_stale = Event()
    calls: list[str] = []
    runner = service._scheduler._runner

    def hold_then_run(task_id: str) -> None:
        calls.append(task_id)
        if len(calls) == 1:
            stale_started.set()
            release_stale.wait(timeout=2)
            return
        runner(task_id)

    service._scheduler._runner = hold_then_run
    try:
        assert service._scheduler.submit(task_id)
        assert stale_started.wait(timeout=2)

        accepted = service.retry_errors(task_id)
        assert accepted["status"] == "running"
        release_stale.set()

        assert _wait_for_status(service, task_id, "completed")
        assert calls == [task_id, task_id]
    finally:
        release_stale.set()
        service.shutdown()


def test_enrich_contacts_resubmits_after_a_stale_future_finishes(tmp_path: Path) -> None:
    discovery = BlockingDiscoveryVerifier()
    discovery.released.set()
    enricher = BlockingEnricher()
    enricher.release.set()
    service = CollectionService(
        TaskStore(tmp_path),
        {"web_search": WorkingProvider()},
        {"deepseek": discovery, "doubao": enricher},
    )
    payload = _payload("enrich-after-stale-future")
    payload["models"] = ["deepseek"]
    task_id = service.create_task(payload)["id"]
    service.store.update(
        task_id,
        lambda task: task.update(
            status="completed",
            completed_city_discovery=["武汉市"],
            leads=[
                {
                    "city": "武汉市",
                    "company_name": "待补全公司",
                    "normalized_company_name": "待补全公司",
                    "matched_keyword": "软装公司",
                }
            ],
        ),
    )
    stale_started = Event()
    release_stale = Event()
    calls: list[str] = []
    runner = service._scheduler._runner

    def hold_then_run(task_id: str) -> None:
        calls.append(task_id)
        if len(calls) == 1:
            stale_started.set()
            release_stale.wait(timeout=2)
            return
        runner(task_id)

    service._scheduler._runner = hold_then_run
    try:
        assert service._scheduler.submit(task_id)
        assert stale_started.wait(timeout=2)

        accepted = service.enrich_contacts(task_id)
        assert accepted["status"] == "running"
        release_stale.set()

        assert _wait_for_status(service, task_id, "completed")
        assert calls == [task_id, task_id]
        assert enricher.calls == ["待补全公司"]
    finally:
        release_stale.set()
        service.shutdown()


def test_unexpected_runner_error_marks_task_completed_with_errors(tmp_path: Path) -> None:
    service = CollectionService(TaskStore(tmp_path), {"web_search": ExplodingProvider()})
    first = service.create_task(_payload("unexpected-error-first"))
    second = service.create_task(_payload("unexpected-error-second"))
    try:
        service.start_task(first["id"])
        assert _wait_for_status(service, first["id"], "completed_with_errors")
        failed = service.get_task(first["id"])
        failure = failed["activity"][-1]
        assert failure["action"] == "run_task"
        assert failure["outcome"] == "error"
        assert failure["detail"] == f"unexpected {'UnexpectedProviderFailure' + 'X' * 200}"[:160]
        assert "secret-token" not in failure["detail"]
        assert len(failure["detail"]) <= 160
        assert failed["updated_at"]

        service.start_task(second["id"])
        assert _wait_for_status(service, second["id"], "completed_with_errors")
    finally:
        service.shutdown()


def test_doubao_parse_error_records_safe_message_and_origin(tmp_path: Path) -> None:
    service = CollectionService(TaskStore(tmp_path), {"web_search": WorkingProvider()})
    task = service.create_task(_payload("doubao-parse-diagnostic"))

    def fail_with_parse_error(task_id: str) -> None:
        del task_id
        raise DoubaoWebResponseError("豆包返回中没有可解析的公司证据")

    service._run_task_serialized = fail_with_parse_error  # type: ignore[method-assign]
    try:
        service.prepare_start(task["id"])
        service.run_task(task["id"])
        failure = service.get_task(task["id"])["activity"][-1]

        assert "unexpected DoubaoWebResponseError" in failure["detail"]
        assert "豆包返回中没有可解析的公司证据" in failure["detail"]
        assert "test_task_scheduler.py:" in failure["detail"]
    finally:
        service.shutdown()
