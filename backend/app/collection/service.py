import json
import re
import traceback
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock, RLock
from time import sleep as system_sleep
from typing import TYPE_CHECKING, Any, ClassVar, Protocol
from uuid import uuid4

from app.collection.cities import tier3_plus_city_names
from app.collection.continuous import (
    DEFAULT_VALID_LEAD_TARGET,
    MATCHED_VERIFICATION_STATUSES,
    ROUND_CANDIDATE_LIMIT,
    ROUND_COOLDOWN_SECONDS,
    TARGET_ROUND_COOLDOWN_SECONDS,
    VERIFIED_COMPANY_NAME_STATUSES,
    candidate_pool_is_sufficient,
    discovery_query_groups,
    new_round_metrics,
    rotate_keywords,
    strict_verified_lead_count,
    target_contact_count,
    target_contact_mode,
    verified_delivery_ineligibility,
    verified_erp_ineligibility,
)
from app.collection.doubao_browser import (
    DoubaoChallengeRequired,
    DoubaoLoginRequired,
    DoubaoWebTimeout,
    DoubaoWebUnavailable,
)
from app.collection.doubao_task_lease import DoubaoTaskLease
from app.collection.doubao_web import DoubaoWebResponseError
from app.collection.erp_delivery import ErpLeadDelivery, build_erp_payload
from app.collection.models import (
    ModelAccountError,
    ModelVerificationError,
    ModelVerifier,
    apply_verification_consensus,
)
from app.collection.normalization import (
    clean_service_intro,
    deduplicate_company_candidates,
    deduplicate_leads,
    limit_lead_emails,
    normalize_company_name,
    normalize_email,
    normalize_emails,
    normalize_lead,
    normalize_mobile,
)
from app.collection.providers import CollectionProvider, ProviderError
from app.collection.scheduler import TaskScheduler

if TYPE_CHECKING:
    from app.collection.doubao_browser import DoubaoWebChatGateway


_EMAIL_PATTERN = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
_DOUBAO_FAILED_RETRY_COOLDOWN_SECONDS = 30 * 60
_DOUBAO_DISCOVERY_BACKOFF_SECONDS = 10 * 60
_DELIVERY_STALE_AFTER_SECONDS = 5 * 60
_DELIVERY_STATE_FIELDS = (
    "delivery_status",
    "delivery_error",
    "delivered_at",
    "delivery_fingerprint",
    "delivery_revision",
    "delivery_operation_id",
    "delivery_started_at",
)


def _delivery_revision(lead: dict[str, Any]) -> int:
    try:
        return max(0, int(lead.get("delivery_revision") or 0))
    except (TypeError, ValueError):
        return 0


def _delivery_identity(lead: dict[str, Any]) -> tuple[str, str]:
    return (
        str(lead.get("city") or "").strip().casefold(),
        (normalize_company_name(lead.get("company_name")) or "").casefold(),
    )


def _copy_delivery_state(source: dict[str, Any], target: dict[str, Any]) -> None:
    for field in _DELIVERY_STATE_FIELDS:
        if field in source:
            target[field] = source[field]
        else:
            target.pop(field, None)


def _merge_delivery_state(current: dict[str, Any], incoming: dict[str, Any]) -> None:
    """Keep a newer or in-flight ERP state when an older task snapshot is saved."""
    current_revision = _delivery_revision(current)
    incoming_revision = _delivery_revision(incoming)
    current_operation = str(current.get("delivery_operation_id") or "")
    incoming_operation = str(incoming.get("delivery_operation_id") or "")
    if current_revision > incoming_revision or (
        current.get("delivery_status") == "sending"
        and current_operation != incoming_operation
    ):
        _copy_delivery_state(current, incoming)


def _advance_delivery_state(
    lead: dict[str, Any],
    status: str,
    *,
    operation_id: str | None = None,
) -> None:
    lead["delivery_revision"] = _delivery_revision(lead) + 1
    lead["delivery_status"] = status
    if operation_id is not None:
        lead["delivery_operation_id"] = operation_id
    elif status != "sending":
        lead.pop("delivery_operation_id", None)
    if status == "sending":
        lead["delivery_started_at"] = _now()
    else:
        lead["delivery_started_at"] = None


def _recover_stale_delivery(lead: dict[str, Any], *, now: datetime | None = None) -> bool:
    """Release an abandoned manual/automatic delivery claim after its lease."""
    if lead.get("delivery_status") != "sending":
        return False
    started_at = str(lead.get("delivery_started_at") or "").strip()
    if not started_at:
        stale = True
    else:
        try:
            started = datetime.fromisoformat(started_at)
            if started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
            stale = (
                (now or datetime.now(UTC)) - started
            ).total_seconds() >= _DELIVERY_STALE_AFTER_SECONDS
        except ValueError:
            stale = True
    if not stale:
        return False
    _advance_delivery_state(lead, "pending")
    lead.update(delivery_error=None, delivered_at=None)
    return True


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _matches_contacts(
    lead: dict[str, Any], required_contacts: list[str], match: str
) -> bool:
    values = [bool(lead.get(field)) for field in required_contacts]
    return all(values) if match == "all" else any(values)


def _candidate_mobile(candidate: dict[str, Any]) -> str | None:
    """Extract a displayed phone without treating it as verified evidence."""
    direct = normalize_mobile(candidate.get("mobile"))
    if direct:
        return direct
    values: list[str] = []
    for key in ("reason", "candidate_reason", "evidence_quote"):
        value = candidate.get(key)
        if isinstance(value, (list, tuple, set)):
            values.extend(str(item) for item in value)
        elif value:
            values.append(str(value))
    values.extend(str(item) for item in candidate.get("evidence_quotes") or [])
    for value in values:
        match = re.search(r"(?<!\d)1[3-9]\d{9}(?!\d)", value)
        if match:
            return match.group(0)
    return None


def _candidate_emails(candidate: dict[str, Any]) -> list[str]:
    """Extract displayed emails without treating them as verified evidence."""
    values: list[str] = []
    for key in ("reason", "candidate_reason", "evidence_quote"):
        value = candidate.get(key)
        if isinstance(value, (list, tuple, set)):
            values.extend(str(item) for item in value)
        elif value:
            values.append(str(value))
    values.extend(str(item) for item in candidate.get("evidence_quotes") or [])
    return normalize_emails(
        [candidate.get("email"), *values],
        company_name=candidate.get("company_name"),
        source_urls=[
            candidate.get("source_url"),
            *(candidate.get("contact_evidence_urls") or []),
            *(candidate.get("company_name_evidence_urls") or []),
        ],
        evidence_quotes=values,
    )


def _candidate_email(candidate: dict[str, Any]) -> str | None:
    emails = _candidate_emails(candidate)
    return emails[0] if emails else None


TASK_DEFAULTS: dict[str, Any] = {
    "use_doubao_web_chat": False,
    "web_chat_discovery": False,
    "web_chat_status": "disabled",
    "web_chat_error": None,
    "web_chat_current_company": None,
    "web_chat_checkpoint": {},
    "web_chat_discovery_failures": 0,
    "web_chat_discovery_retry_after": None,
    "auto_deliver": False,
    "continuous_mode": False,
    "valid_lead_target": None,
    "verified_auto_deliver": False,
    "round_number": 1,
    "round_phase": None,
    "round_started_at": None,
    "next_round_at": None,
    "pause_requested": False,
    "terminate_requested": False,
    "started_at": None,
    "paused_at": None,
    "terminated_at": None,
    "completed_at": None,
    "pause_reason": None,
    "last_blocking_error": None,
}
LEAD_DELIVERY_DEFAULTS: dict[str, Any] = {
    "delivery_status": "display_only",
    "delivery_error": None,
    "delivered_at": None,
    "delivery_fingerprint": None,
    "delivery_started_at": None,
}
COMPANY_NAME_VERIFICATION_DEFAULTS: dict[str, Any] = {
    "company_name_verification_status": "unverified",
    "company_name_evidence_urls": [],
    "company_name_verification_reason": "",
    "company_name_verified_at": None,
    "company_name_verification_attempts": 0,
}
CONTACT_VERIFICATION_DEFAULTS: dict[str, Any] = {
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
TERMINAL_ENRICHMENT_STATES = {"completed", "failed", "blocked"}
COMPANY_VERIFICATION_BATCH_SIZE = 20
# Bound public fallback work per round; rotated keywords continue next round.
PUBLIC_FALLBACK_KEYWORDS_PER_ROUND = 6
VERIFIED_CONTACT_STATUSES = {
    "verified_public_web",
    "verified_official_page",
    "verified_cross_source",
}


def _with_task_defaults(task: dict[str, Any]) -> dict[str, Any]:
    for key, value in TASK_DEFAULTS.items():
        task.setdefault(key, value.copy() if isinstance(value, (dict, list)) else value)
    if task.get("continuous_mode") and task.get("valid_lead_target") is None:
        task["valid_lead_target"] = int(
            task.get("limit_per_city") or DEFAULT_VALID_LEAD_TARGET
        )
    task.setdefault("round_metrics", new_round_metrics())
    task.setdefault("round_checkpoint", {})
    for lead in task.get("leads", []):
        if lead.get("service_intro"):
            lead["service_intro"] = clean_service_intro(lead.get("service_intro"))
        for key, value in LEAD_DELIVERY_DEFAULTS.items():
            lead.setdefault(key, value)
        for key, value in COMPANY_NAME_VERIFICATION_DEFAULTS.items():
            lead.setdefault(key, value.copy() if isinstance(value, list) else value)
        for key, value in CONTACT_VERIFICATION_DEFAULTS.items():
            lead.setdefault(key, value.copy() if isinstance(value, (dict, list)) else value)
    return task


class TaskNotFoundError(LookupError):
    pass


class InvalidTaskStateError(RuntimeError):
    pass


class TaskPaused(RuntimeError):
    pass


class TaskTerminated(RuntimeError):
    pass


class TaskWaitingForLogin(RuntimeError):
    pass


class LeadDelivery(Protocol):
    def deliver(self, task: dict[str, Any], lead: dict[str, Any]) -> Any: ...

    def retry_results(self, task_id: str | None = None) -> list[Any]: ...


class CompanyNameVerifier(Protocol):
    def verify_company_name(
        self,
        *,
        company_name: str,
        city: str,
        keywords: list[str],
        source_urls: list[str] | None = None,
    ) -> Any: ...


class ContactVerifier(Protocol):
    def verify_contact_evidence(
        self,
        *,
        company_name: str,
        mobile: str | None,
        source_url: str | None,
        contact_person_name: str | None,
        contact_person_role: str | None,
    ) -> Any: ...


class TaskStore:
    def __init__(self, data_dir: Path) -> None:
        self.tasks_dir = data_dir / "tasks"
        self.exports_dir = data_dir / "exports"
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self.exports_dir.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()

    def load(self, task_id: str, *, include_leads: bool = True) -> dict[str, Any]:
        path = self.tasks_dir / f"{task_id}.json"
        with self._lock:
            try:
                task = _with_task_defaults(json.loads(path.read_text(encoding="utf-8")))
            except FileNotFoundError as exc:
                raise TaskNotFoundError(task_id) from exc
        for lead in task.get("leads", []):
            limit_lead_emails(lead)
            _recover_stale_delivery(lead)
        if not include_leads:
            task["leads"] = []
        return task

    def save(self, task: dict[str, Any]) -> None:
        path = self.tasks_dir / f"{task['id']}.json"
        temporary = path.with_suffix(".tmp")
        for lead in task.get("leads", []):
            limit_lead_emails(lead)
        with self._lock:
            temporary.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(path)

    def save_leads(self, task_id: str, leads: list[dict[str, Any]]) -> None:
        with self._lock:
            task = self.load(task_id)
            indexed = {
                (lead.get("city"), lead.get("normalized_company_name") or lead.get("company_name", "").casefold()): lead
                for lead in task.get("leads", [])
            }
            for lead in leads:
                key = (
                    lead.get("city"),
                    lead.get("normalized_company_name") or lead.get("company_name", "").casefold(),
                )
                current = indexed.get(key)
                if current is not None:
                    _merge_delivery_state(current, lead)
                indexed[key] = lead
            task["leads"] = list(indexed.values())
            self.save(task)

    def reconcile_leads(
        self,
        task_id: str,
        city: str,
        leads: list[dict[str, Any]],
    ) -> None:
        if any(lead.get("city") != city for lead in leads):
            raise ValueError("reconciled leads must belong to the selected city")
        with self._lock:
            task = self.load(task_id)
            task["leads"] = [
                lead for lead in task.get("leads", []) if lead.get("city") != city
            ] + list(leads)
            self.save(task)

    def update(self, task_id: str, mutation: Callable[[dict[str, Any]], Any]) -> dict[str, Any]:
        with self._lock:
            task = self.load(task_id)
            mutation(task)
            self.save(task)
            return task

    def list_tasks(self) -> list[dict[str, Any]]:
        with self._lock:
            tasks = [
                _with_task_defaults(json.loads(path.read_text(encoding="utf-8")))
                for path in self.tasks_dir.glob("*.json")
            ]
            for task in tasks:
                for lead in task.get("leads", []):
                    limit_lead_emails(lead)
                    _recover_stale_delivery(lead)
        return sorted(tasks, key=lambda task: task.get("created_at", ""), reverse=True)

    def list_leads(
        self,
        task_id: str,
        city: str | None = None,
        offset: int = 0,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        leads = sorted(
            self.load(task_id).get("leads", []),
            key=lambda lead: (lead.get("saved_at") or "", lead.get("id") or ""),
        )
        if city is not None:
            leads = [lead for lead in leads if lead.get("city") == city]
        end = None if limit is None else offset + limit
        return leads[offset:end]

    def count_leads(self, task_id: str, city: str | None = None) -> int:
        leads = self.load(task_id).get("leads", [])
        if city is None:
            return len(leads)
        return sum(lead.get("city") == city for lead in leads)

    def iter_leads(self, task_id: str, batch_size: int = 500):
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        leads = sorted(
            self.load(task_id).get("leads", []),
            key=lambda lead: (lead.get("saved_at") or "", lead.get("id") or ""),
        )
        for offset in range(0, len(leads), batch_size):
            yield from leads[offset : offset + batch_size]

    def list_task_ids_by_status(self, statuses: list[str]) -> list[str]:
        selected = set(statuses)
        if not selected:
            return []
        with self._lock:
            return [
                path.stem
                for path in sorted(self.tasks_dir.glob("*.json"))
                if json.loads(path.read_text(encoding="utf-8")).get("status")
                in selected
            ]


class CollectionService:
    MODEL_CATALOG: ClassVar[tuple[tuple[str, str], ...]] = (
        ("deepseek", "DeepSeek"),
        ("doubao", "豆包"),
        ("qwen", "通义千问"),
        ("kimi", "Kimi"),
        ("openai", "OpenAI"),
        ("claude", "Claude"),
        ("gemini", "Gemini"),
    )
    def __init__(
        self,
        store: TaskStore,
        providers: dict[str, CollectionProvider],
        verifiers: dict[str, ModelVerifier] | None = None,
        *,
        company_name_verifier: CompanyNameVerifier | None = None,
        contact_verifier: ContactVerifier | None = None,
        lead_delivery: LeadDelivery | None = None,
        doubao_web_gateway: "DoubaoWebChatGateway | None" = None,
        max_concurrent_tasks: int = 2,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.store = store
        self.providers = providers
        self.verifiers = verifiers or {}
        self.company_name_verifier = company_name_verifier
        self.contact_verifier = contact_verifier
        self.lead_delivery = lead_delivery
        self.doubao_web_gateway = doubao_web_gateway
        self._now = now or (lambda: datetime.now(UTC))
        self._sleep = sleep or system_sleep
        self._operation_locks_guard = Lock()
        self._operation_locks: dict[str, Any] = {}
        self._candidate_reservation_lock = Lock()
        self._doubao_task_lease = DoubaoTaskLease()
        self._scheduler = TaskScheduler(
            self._run_scheduled_task,
            max_workers=max_concurrent_tasks,
        )
        self._recover_interrupted_deliveries()
        self._backfill_delivery_ledger()
        self._reserve_waiting_doubao_task()
        self._recover_interrupted_tasks()

    def _task_operation_lock(self, task_id: str) -> Any:
        with self._operation_locks_guard:
            return self._operation_locks.setdefault(task_id, RLock())

    def _reserved_candidate_keys(self, *, task_id: str, city: str) -> set[tuple[str, str]]:
        city_key = city.strip().casefold()
        reserved: set[tuple[str, str]] = set()
        for other in self.store.list_tasks():
            # A company discovered by any earlier task remains reserved even
            # after that task reaches a terminal state. This keeps later
            # continuous runs from recycling historical candidates.
            if other.get("id") == task_id:
                continue
            for lead in other.get("leads", []):
                if str(lead.get("city") or city).strip().casefold() != city_key:
                    continue
                company_name = normalize_company_name(lead.get("company_name"))
                if company_name:
                    reserved.add((city_key, company_name.casefold()))
        return reserved

    def _prune_historical_candidate_duplicates(self, task: dict[str, Any]) -> int:
        """Remove candidates that were already seen by another task.

        Continuous tasks can be resumed after a code update or process restart,
        so reservation at discovery time is not enough to clean candidates
        persisted by an older run. Keep the task's own leads, but discard any
        same-city company that exists in another task's history.
        """
        with self._candidate_reservation_lock:
            original = list(task.get("leads", []))
            deduplicated = deduplicate_leads(original)
            reserved_by_city: dict[str, set[tuple[str, str]]] = {}
            retained: list[dict[str, Any]] = []
            for lead in deduplicated:
                city = str(lead.get("city") or "").strip()
                company_name = normalize_company_name(lead.get("company_name"))
                if not city or not company_name:
                    retained.append(lead)
                    continue
                city_key = city.casefold()
                reserved = reserved_by_city.setdefault(
                    city_key,
                    self._reserved_candidate_keys(task_id=task["id"], city=city),
                )
                if (city_key, company_name.casefold()) in reserved:
                    continue
                retained.append(lead)

            removed = len(original) - len(retained)
            if not removed and len(deduplicated) == len(original):
                return 0
            task["leads"] = retained
            task["lead_count"] = len(retained)
            task["candidate_count"] = len(retained)
            task["enrichment_total"] = len(retained)
            task["enrichment_completed"] = sum(
                lead.get("enrichment_state") in TERMINAL_ENRICHMENT_STATES
                for lead in retained
            )
            if removed:
                task.setdefault("activity", []).append(
                    self._activity(
                        "prune_historical_duplicates",
                        outcome="accepted",
                        detail=f"removed {removed} candidates already seen in previous tasks",
                    )
                )
            task["updated_at"] = _now()
            self._save_progress(task)
            return removed

    def _recover_interrupted_deliveries(self) -> None:
        for task in self.store.list_tasks():
            if not any(
                lead.get("delivery_status") == "sending"
                for lead in task.get("leads", [])
            ):
                continue

            def recover(current: dict[str, Any]) -> None:
                for lead in current.get("leads", []):
                    if lead.get("delivery_status") == "sending":
                        _advance_delivery_state(lead, "pending")
                        lead.update(delivery_error=None, delivered_at=None)
                current["updated_at"] = _now()

            self.store.update(task["id"], recover)

    def _backfill_delivery_ledger(self) -> None:
        """Record successful historical leads before accepting new candidates."""
        ledger = getattr(self.lead_delivery, "dedup_ledger", None)
        evaluate = getattr(ledger, "evaluate", None)
        mark_delivered = getattr(ledger, "mark_delivered", None)
        if not callable(evaluate) or not callable(mark_delivered):
            return
        for task in self.store.list_tasks():
            for lead in task.get("leads", []):
                if lead.get("delivery_status") != "succeeded":
                    continue
                company_name = normalize_company_name(lead.get("company_name"))
                if not company_name:
                    continue
                mobile = normalize_mobile(lead.get("mobile"))
                email = normalize_email(lead.get("email"))
                if not mobile and not email:
                    continue
                try:
                    decision = evaluate(
                        city=str(lead.get("city") or ""),
                        company_name=company_name,
                        mobile=mobile,
                        email=email,
                        evidence_score=int(lead.get("contact_evidence_score") or 0),
                    )
                except (TypeError, ValueError, KeyError):
                    continue
                if decision.action in {"create", "update"}:
                    mark_delivered(decision)

    def _recover_interrupted_tasks(self) -> None:
        tasks = sorted(
            self.store.list_tasks(),
            key=lambda task: task.get("created_at", ""),
        )
        for task in tasks:
            if task.get("status") not in {"queued", "running", "pausing", "terminating"}:
                continue
            task_id = task["id"]
            if task.get("terminate_requested"):
                self._mark_terminated(task_id)
                continue
            if task.get("pause_requested"):
                self._mark_paused(task_id)
                continue
            self.store.update(
                task_id,
                lambda current: current.update(status="queued", updated_at=_now()),
            )
            self._scheduler.submit(task_id)

    def _reserve_waiting_doubao_task(self) -> None:
        tasks = sorted(
            self.store.list_tasks(),
            key=lambda task: task.get("created_at", ""),
        )
        for task in tasks:
            if task.get("status") == "waiting_for_login" and task.get(
                "use_doubao_web_chat"
            ):
                self._doubao_task_lease.reserve(task["id"])
                return

    def provider_items(self) -> list[dict[str, Any]]:
        return [
            {"id": provider.id, "label": provider.label, "configured": provider.configured}
            for provider in self.providers.values()
        ]

    def model_items(self) -> list[dict[str, Any]]:
        items = []
        for model_id, label in self.MODEL_CATALOG:
            verifier = self.verifiers.get(model_id)
            items.append(
                {
                    "id": model_id,
                    "label": verifier.label if verifier else label,
                    "configured": bool(verifier and verifier.configured),
                }
            )
        return items

    def city_names(self) -> list[str]:
        return tier3_plus_city_names()

    def create_task(self, payload: dict[str, Any], *, created_by: str = "local-user") -> dict[str, Any]:
        timestamp = _now()
        total_per_city = len(payload["keywords"]) * len(payload["providers"])
        task = {
            "id": str(uuid4()),
            **deepcopy(TASK_DEFAULTS),
            **payload,
            "status": "draft",
            "total_units": len(payload["cities"]) * total_per_city,
            "completed_units": 0,
            "lead_count": 0,
            "city_progress": {
                city: {
                    "city": city,
                    "status": "pending",
                    "total_units": total_per_city,
                    "completed_units": 0,
                    "lead_count": 0,
                }
                for city in payload["cities"]
            },
            "created_by": created_by,
            "created_at": timestamp,
            "updated_at": timestamp,
            "completed_unit_keys": [],
            "leads": [],
            "activity": [self._activity("created", outcome="completed")],
            "unit_errors": 0,
            "empty_units": 0,
            "verification_errors": 0,
            "verification_state": "not_started",
            "collection_phase": "discovering",
            "candidate_target": len(payload["cities"])
            * (payload.get("limit_per_city") or 100),
            "candidate_count": 0,
            "enrichment_total": 0,
            "enrichment_completed": 0,
            "completed_city_discovery": [],
            "round_metrics": new_round_metrics(),
            "round_checkpoint": {},
        }
        if task.get("continuous_mode") and task.get("valid_lead_target") is None:
            task["valid_lead_target"] = int(
                task.get("limit_per_city") or DEFAULT_VALID_LEAD_TARGET
            )
        self.store.save(task)
        return task

    @staticmethod
    def _hydrate_candidate_contacts_in_place(task: dict[str, Any]) -> bool:
        """Expose contacts already present in a discovery reason without verifying them."""
        changed = False
        for lead in task.get("leads", []):
            if not normalize_mobile(lead.get("mobile")):
                mobile = _candidate_mobile(lead)
                if mobile:
                    lead["mobile"] = mobile
                    changed = True
            if not normalize_email(lead.get("email")):
                email = _candidate_email(lead)
                if email:
                    lead["email"] = email
                    changed = True
        return changed

    def get_task(self, task_id: str) -> dict[str, Any]:
        task = self.store.load(task_id)
        if self._hydrate_candidate_contacts_in_place(task):
            task["updated_at"] = _now()
            self.store.save(task)
        return task

    def get_task_metadata(self, task_id: str) -> dict[str, Any]:
        return self.store.load(task_id, include_leads=False)

    def start_task(self, task_id: str) -> dict[str, Any]:
        def queue(task: dict[str, Any]) -> None:
            if task["status"] in {
                "queued",
                "running",
                "pausing",
                "completed",
                "completed_with_errors",
                "terminated",
            }:
                return
            task["status"] = "queued"
            task["pause_requested"] = False
            task["terminate_requested"] = False
            task["updated_at"] = _now()
            task.setdefault("activity", []).append(
                self._activity("started", outcome="accepted")
            )

        task = self.store.update(task_id, queue)
        if task["status"] == "queued":
            self._scheduler.submit(task_id)
        return task

    def pause_task(self, task_id: str) -> dict[str, Any]:
        def pause(task: dict[str, Any]) -> None:
            if task["status"] in {"completed", "completed_with_errors"}:
                raise InvalidTaskStateError("completed collection task cannot be paused")
            if task["status"] == "waiting_for_login":
                raise InvalidTaskStateError(
                    "collection task waiting for Doubao login cannot be paused"
                )
            if task["status"] in {
                "paused",
                "terminated",
            }:
                return
            task["pause_requested"] = True
            task["updated_at"] = _now()
            if task["status"] in {"draft", "queued"}:
                task["status"] = "paused"
                task["paused_at"] = _now()
            else:
                task["status"] = "pausing"

        return self.store.update(task_id, pause)

    def resume_task(self, task_id: str) -> dict[str, Any]:
        def resume(task: dict[str, Any]) -> None:
            if task["status"] in {"completed", "completed_with_errors"}:
                raise InvalidTaskStateError("completed collection task cannot be resumed")
            if task["status"] == "waiting_for_login":
                gateway = self.doubao_web_gateway
                try:
                    gateway_ready = gateway is not None and gateway.refresh_status().state == "ready"
                except (DoubaoLoginRequired, DoubaoChallengeRequired, DoubaoWebUnavailable):
                    gateway_ready = False
                if not gateway_ready:
                    raise InvalidTaskStateError("豆包网页尚未完成登录")
                task["status"] = "queued"
                task["web_chat_status"] = "ready"
                task["web_chat_error"] = None
                task["pause_requested"] = False
                task["updated_at"] = _now()
                task.setdefault("activity", []).append(
                    self._activity("resumed_after_login", outcome="accepted")
                )
                return
            if task["status"] == "running":
                # A backend restart can leave a persisted task marked running
                # after its in-memory future was lost. Clearing a stale pause
                # flag and re-submitting is idempotent: an active scheduler
                # entry is rejected, while an orphaned task is recovered.
                task["pause_requested"] = False
                task["updated_at"] = _now()
                task.setdefault("activity", []).append(
                    self._activity(
                        "resume_checked",
                        outcome="accepted",
                        detail="checked the running task and requeued it only when no active worker exists",
                    )
                )
                return
            if task["status"] != "paused":
                return
            task["status"] = "queued"
            task["pause_requested"] = False
            task["updated_at"] = _now()
            task.setdefault("activity", []).append(
                self._activity("resumed", outcome="accepted")
            )

        task = self.store.update(task_id, resume)
        if task["status"] in {"queued", "running"} and not task.get("pause_requested"):
            # submit() is a no-op when the task already has an active future.
            # This makes the running-task recovery path safe to click repeatedly.
            self._scheduler.submit(task_id)
        return task

    def terminate_task(self, task_id: str) -> dict[str, Any]:
        def terminate(task: dict[str, Any]) -> None:
            if task["status"] in {"terminated", "completed", "completed_with_errors"}:
                return
            task["terminate_requested"] = True
            task["updated_at"] = _now()
            if task["status"] in {"draft", "queued", "paused", "waiting_for_login"}:
                task["status"] = "terminated"
                task["terminated_at"] = _now()
            else:
                task["status"] = "terminating"

        task = self.store.update(task_id, terminate)
        if task.get("terminate_requested"):
            self._scheduler.cancel_resubmit(task_id)
        if task.get("status") == "terminated":
            self._doubao_task_lease.release(task_id)
        return task

    def shutdown(self) -> None:
        self._doubao_task_lease.shutdown()
        self._scheduler.shutdown()
        if self.doubao_web_gateway is not None:
            self.doubao_web_gateway.shutdown()

    @staticmethod
    def task_summary(task: dict[str, Any]) -> dict[str, Any]:
        internal_fields = {
            "leads",
            "activity",
            "completed_unit_keys",
            "_persisted_activity_count",
        }
        summary = {key: value for key, value in task.items() if key not in internal_fields}
        for key, value in TASK_DEFAULTS.items():
            summary.setdefault(key, value)
        summary.setdefault("empty_units", 0)
        summary.setdefault("verification_state", "not_started")
        summary.setdefault("collection_phase", "completed" if task.get("status") == "completed" else "discovering")
        summary.setdefault("candidate_target", len(task.get("cities", [])) * (task.get("limit_per_city") or 100))
        summary.setdefault("candidate_count", len(task.get("leads", [])))
        summary.setdefault("enrichment_total", len(task.get("leads", [])))
        summary.setdefault(
            "enrichment_completed",
            sum(
                lead.get("enrichment_state") in TERMINAL_ENRICHMENT_STATES
                for lead in task.get("leads", [])
            ),
        )
        summary["company_name_verified_count"] = sum(
            lead.get("company_name_verification_status") in VERIFIED_COMPANY_NAME_STATUSES
            for lead in task.get("leads", [])
        )
        summary["company_name_unverified_count"] = sum(
            bool(lead.get("company_name_verified_at"))
            and lead.get("company_name_verification_status") not in VERIFIED_COMPANY_NAME_STATUSES
            for lead in task.get("leads", [])
        )
        summary["raw_contact_lead_count"] = sum(
            bool(normalize_mobile(lead.get("mobile")) or normalize_email(lead.get("email")))
            for lead in task.get("leads", [])
        )
        strict_count = strict_verified_lead_count(task.get("leads", []))
        summary["strict_contact_lead_count"] = strict_count
        summary["target_contact_mode"] = target_contact_mode(task)
        summary["target_contact_lead_count"] = target_contact_count(task)
        summary["contact_lead_count"] = summary["target_contact_lead_count"]
        if summary.get("status") == "completed" and not task.get("leads"):
            summary["status"] = "completed_with_errors"
        return summary

    def prepare_start(self, task_id: str) -> tuple[dict[str, Any], bool]:
        task = self.store.load(task_id)
        if task["status"] in {"running", "completed", "completed_with_errors"}:
            return task, False
        task["status"] = "running"
        task["updated_at"] = _now()
        task["activity"].append(self._activity("started", outcome="accepted"))
        self.store.save(task)
        return task, True

    def prepare_contact_enrichment(self, task_id: str) -> tuple[dict[str, Any], bool]:
        task = self.store.load(task_id)
        if task["status"] == "running" or not task.get("leads"):
            return task, False
        for lead in task["leads"]:
            if (
                self.company_name_verifier is not None
                and lead.get("company_name_verification_status")
                not in VERIFIED_COMPANY_NAME_STATUSES
            ):
                lead["enrichment_state"] = "blocked"
                lead["enrichment_error"] = lead.get("company_name_verification_reason")
                continue
            lead["enrichment_state"] = "pending"
            lead["enrichment_model"] = None
            lead.pop("enrichment_error", None)
        self.store.save_leads(task_id, task["leads"])
        task["enrichment_completed"] = 0
        task["unit_errors"] = 0
        task["collection_phase"] = "enriching"
        task["status"] = "running"
        task["updated_at"] = _now()
        task["activity"].append(
            self._activity(
                "reenrich_contacts",
                outcome="accepted",
                detail=f"requeued {len(task['leads'])} candidates for doubao",
            )
        )
        self.store.save(task)
        return task, True

    def enrich_contacts(self, task_id: str) -> dict[str, Any]:
        task, should_run = self.prepare_contact_enrichment(task_id)
        if should_run:
            self._scheduler.submit(task_id, resubmit_if_running=True)
        return task

    def deliver_lead(
        self,
        task_id: str,
        *,
        city: str,
        company_name: str,
        target_url: str,
        bearer_token: str | None = None,
        client: Any | None = None,
    ) -> dict[str, Any]:
        """Deliver one user-selected lead without enabling automatic delivery."""
        requested_city = city.strip()
        requested_name = normalize_company_name(company_name)
        if not requested_city or not requested_name:
            raise ValueError("城市和公司名称不能为空")
        operation_id = str(uuid4())

        def claim(task: dict[str, Any]) -> None:
            lead = next(
                (
                    item
                    for item in task.get("leads", [])
                    if str(item.get("city") or "").strip().casefold()
                    == requested_city.casefold()
                    and normalize_company_name(item.get("company_name"))
                    and normalize_company_name(item.get("company_name")).casefold()
                    == requested_name.casefold()
                ),
                None,
            )
            if lead is None:
                raise ValueError("未找到对应的采集结果")
            if lead.get("delivery_status") == "sending":
                raise ValueError("该信息正在推送中，请稍候")
            if lead.get("delivery_status") == "succeeded":
                return
            mobile = normalize_mobile(lead.get("mobile"))
            emails = normalize_emails(
                lead.get("email"),
                company_name=lead.get("company_name"),
                source_urls=[
                    lead.get("source_url"),
                    *(lead.get("contact_evidence_urls") or []),
                ],
                evidence_quotes=[
                    lead.get("contact_evidence_quote"),
                    *(lead.get("contact_evidence_quotes") or []),
                ],
            )
            email = "; ".join(emails) or None
            if not mobile and not email:
                raise ValueError("需要有效手机号或邮箱才能推送")
            lead["mobile"] = mobile
            lead["email"] = email
            _advance_delivery_state(lead, "sending", operation_id=operation_id)
            lead.update(delivery_error=None, delivered_at=None)

        task = self.store.update(task_id, claim)
        lead = next(
            item
            for item in task.get("leads", [])
            if _delivery_identity(item)
            == (requested_city.casefold(), requested_name.casefold())
        )
        if lead.get("delivery_status") == "succeeded":
            return {"status": "succeeded", "lead": lead}

        try:
            if client is not None:
                delivery = ErpLeadDelivery(
                    self.store.tasks_dir.parent,
                    target_url,
                    client=client,
                    bearer_token=bearer_token,
                )
                result = delivery.deliver(task, lead)
            elif isinstance(self.lead_delivery, ErpLeadDelivery):
                result = self.lead_delivery.deliver(
                    task,
                    lead,
                    target_url=target_url,
                    bearer_token=bearer_token,
                )
            else:
                delivery = ErpLeadDelivery(
                    self.store.tasks_dir.parent,
                    target_url,
                    bearer_token=bearer_token,
                )
                result = delivery.deliver(task, lead)
            status_value = str(getattr(result, "status", "failed"))
            error = getattr(result, "error", None)
            fingerprint = getattr(result, "fingerprint", None)
        except Exception as exc:  # noqa: BLE001 - a failed row must not affect the task
            status_value = "failed"
            error = str(exc)
            fingerprint = None
        succeeded = status_value in {"completed", "skipped"}
        result_lead: dict[str, Any] = {}

        def finish(current: dict[str, Any]) -> None:
            nonlocal result_lead
            current_lead = next(
                (
                    item
                    for item in current.get("leads", [])
                    if _delivery_identity(item)
                    == (requested_city.casefold(), requested_name.casefold())
                ),
                None,
            )
            if current_lead is None:
                raise ValueError("未找到对应的采集结果")
            if current_lead.get("delivery_operation_id") == operation_id:
                _advance_delivery_state(
                    current_lead,
                    "succeeded" if succeeded else "failed",
                    operation_id=operation_id,
                )
                current_lead.update(
                    delivery_error=(
                        None if succeeded else (str(error) if error else "ERP 推送失败")
                    ),
                    delivered_at=_now() if succeeded else None,
                    delivery_fingerprint=fingerprint,
                )
                current.setdefault("activity", []).append(
                    self._activity(
                        "manual_erp_delivery",
                        city=requested_city,
                        provider="erp",
                        outcome="completed" if succeeded else "failed",
                        detail=str(error)[:500] if error else None,
                    )
                )
                current["updated_at"] = _now()
            result_lead = current_lead

        self.store.update(task_id, finish)
        return {
            "status": "succeeded" if succeeded else "failed",
            "lead": result_lead,
            **({"error": str(error)} if not succeeded and error else {}),
        }

    @staticmethod
    def _delivery_contact(lead: dict[str, Any]) -> tuple[str | None, str | None]:
        return normalize_mobile(lead.get("mobile")), normalize_email(lead.get("email"))

    @classmethod
    def _delivery_candidate_score(cls, lead: dict[str, Any]) -> tuple[int, int, int, int, int, str]:
        mobile, email = cls._delivery_contact(lead)
        verified = (
            lead.get("company_name_verification_status") in VERIFIED_COMPANY_NAME_STATUSES
            and lead.get("contact_verification_status") in VERIFIED_CONTACT_STATUSES
        )
        evidence = int(lead.get("contact_evidence_score") or 0)
        completeness = sum(
            bool(lead.get(field))
            for field in (
                "mobile",
                "email",
                "wechat",
                "contact_person_name",
                "contact_person_role",
                "address",
                "service_intro",
                "source_url",
                "contact_evidence_url",
            )
        )
        return (
            int(verified),
            int(bool(mobile or email)),
            evidence,
            completeness,
            int(bool(mobile)),
            str(lead.get("company_name") or ""),
        )

    @classmethod
    def select_delivery_leads(
        cls,
        task: dict[str, Any],
        *,
        max_items: int = 100,
    ) -> list[dict[str, Any]]:
        """Select a stable, non-duplicating ERP batch.

        Verified contacts are preferred. If fewer than the requested amount are
        verified, candidates with a usable phone or email are included in
        descending information completeness order. Successful rows never re-enter
        a later batch.
        """
        candidates = [
            lead
            for lead in task.get("leads", [])
            if lead.get("company_name")
            and lead.get("delivery_status") != "succeeded"
            and any(cls._delivery_contact(lead))
        ]
        return sorted(candidates, key=cls._delivery_candidate_score, reverse=True)[: max(0, int(max_items))]

    def deliver_task(
        self,
        task_id: str,
        *,
        target_url: str,
        bearer_token: str | None = None,
        max_items: int = 100,
        client: Any | None = None,
    ) -> dict[str, int | str]:
        """Push selected leads individually and persist every terminal outcome."""
        with self._task_operation_lock(task_id):
            task = self.store.load(task_id)
            selected = self.select_delivery_leads(task, max_items=max_items)
            sent = failed = 0
            for lead in selected:
                try:
                    result = self.deliver_lead(
                        task_id,
                        city=str(lead.get("city") or ""),
                        company_name=str(lead.get("company_name") or ""),
                        target_url=target_url,
                        bearer_token=bearer_token,
                        client=client,
                    )
                except (TaskNotFoundError, ValueError) as exc:
                    failed += 1
                    self.store.update(
                        task_id,
                        lambda current, lead=lead, error=str(exc): self._mark_delivery_failure(
                            current, lead, error
                        ),
                    )
                    continue
                if result.get("status") == "succeeded":
                    sent += 1
                else:
                    failed += 1
            return {
                "status": "completed",
                "selected": len(selected),
                "sent": sent,
                "failed": failed,
                "batches": sent + failed,
            }

    @staticmethod
    def _mark_delivery_failure(
        task: dict[str, Any], lead: dict[str, Any], error: str
    ) -> None:
        for current in task.get("leads", []):
            if (
                current.get("city") == lead.get("city")
                and normalize_company_name(current.get("company_name"))
                == normalize_company_name(lead.get("company_name"))
            ):
                current.update(
                    delivery_status="failed",
                    delivery_error=error,
                    delivered_at=None,
                )
                break
        task["updated_at"] = _now()

    def prepare_retry_errors(self, task_id: str) -> tuple[dict[str, Any], bool]:
        task = self.store.load(task_id)
        legacy_zero_result = task["status"] == "completed" and not task.get("leads")
        if task["status"] != "completed_with_errors" and not legacy_zero_result:
            return task, False
        latest_outcomes: dict[str, str] = {}
        for activity in task["activity"]:
            if activity.get("action") != "collect_unit":
                continue
            unit_key = json.dumps(
                [activity.get("city"), activity.get("keyword"), activity.get("provider")], ensure_ascii=False
            )
            latest_outcomes[unit_key] = activity.get("outcome", "error")
        failed_keys = {
            key for key, outcome in latest_outcomes.items() if outcome in {"error", "empty"}
        }
        if legacy_zero_result:
            failed_keys = set(task["completed_unit_keys"])
        completed_keys = set(task["completed_unit_keys"]) - failed_keys
        task["completed_unit_keys"] = list(completed_keys)
        task["completed_units"] = len(completed_keys)
        task["unit_errors"] = 0
        task["empty_units"] = 0
        task["status"] = "running"
        task["updated_at"] = _now()
        task["activity"].append(
            self._activity("retry_errors", outcome="accepted", detail=f"requeued {len(failed_keys)} units")
        )
        for city in task["cities"]:
            self._update_city_progress(task, city)
        self.store.save(task)
        return task, len(completed_keys) < task["total_units"]

    def retry_errors(self, task_id: str) -> dict[str, Any]:
        task, should_run = self.prepare_retry_errors(task_id)
        if should_run:
            self._scheduler.submit(task_id, resubmit_if_running=True)
        return task

    def retry_delivery(self, task_id: str) -> dict[str, int]:
        with self._task_operation_lock(task_id):
            return self._retry_delivery_locked(task_id)

    def _retry_delivery_locked(self, task_id: str) -> dict[str, int]:
        task = self.store.load(task_id)
        if self.lead_delivery is None:
            return {"attempted": 0, "succeeded": 0, "failed": 0}
        if task.get("verified_auto_deliver"):
            attempted = 0
            succeeded = 0
            failed = 0
            for lead in task.get("leads", []):
                if lead.get("delivery_status") != "failed":
                    continue
                reason = verified_delivery_ineligibility(lead)
                if reason:
                    lead.update(
                        delivery_status="display_only",
                        delivery_error=reason,
                        delivered_at=None,
                        delivery_fingerprint=None,
                    )
                    self.store.save_leads(task_id, [lead])
                    continue
                attempted += 1
                lead["delivery_status"] = "pending"
                self.store.save_leads(task_id, [lead])
                self._send_lead(task, lead, check_after=False)
                if lead.get("delivery_status") == "succeeded":
                    succeeded += 1
                else:
                    failed += 1
            return {
                "attempted": attempted,
                "succeeded": succeeded,
                "failed": failed,
            }
        results = self.lead_delivery.retry_results(task_id)
        results_by_guid = {result.customer_guid: result for result in results}

        def apply_results(current: dict[str, Any]) -> None:
            for lead in current.get("leads", []):
                guid = build_erp_payload(current, lead)["customerAccountGuid"]
                result = results_by_guid.get(guid)
                if result is None:
                    continue
                succeeded = result.status in {"completed", "skipped"}
                lead.update(
                    delivery_status="succeeded" if succeeded else "failed",
                    delivery_error=None if succeeded else result.error,
                    delivered_at=_now() if succeeded else None,
                    delivery_fingerprint=result.fingerprint,
                )
            current["updated_at"] = _now()

        self.store.update(task_id, apply_results)
        return {
            "attempted": len(results),
            "succeeded": sum(
                result.status in {"completed", "skipped"} for result in results
            ),
            "failed": sum(result.status == "failed" for result in results),
        }

    def prepare_verification(self, task_id: str) -> tuple[dict[str, Any], bool]:
        with self._task_operation_lock(task_id):
            return self._prepare_verification_locked(task_id)

    def _prepare_verification_locked(self, task_id: str) -> tuple[dict[str, Any], bool]:
        task = self.store.load(task_id)
        if task.get("verification_state") == "running" or not task.get("leads"):
            return task, False
        task["verification_state"] = "running"
        task["verification_errors"] = 0
        task["updated_at"] = _now()
        task["activity"].append(self._activity("verification_started", outcome="accepted"))
        self.store.save(task)
        return task, True

    def verify_task(self, task_id: str) -> None:
        with self._task_operation_lock(task_id):
            self._verify_task_locked(task_id)

    def _verify_task_locked(self, task_id: str) -> None:
        task = self.store.load(task_id)
        if task.get("use_doubao_web_chat") and task.get("web_chat_discovery"):
            verified: list[dict[str, Any]] = []
            for lead in task["leads"]:
                evidence_candidate = {**lead, "verification_status": "cross_verified"}
                matched = verified_erp_ineligibility(evidence_candidate) is None
                verified.append(
                    {
                        **lead,
                        "verification_status": (
                            "cross_verified" if matched else "single_model_reject"
                        ),
                        "verification_models": ["doubao"],
                        "verification_detail": {
                            "doubao": {
                                "relevant": matched,
                                "reason": (
                                    "公司名称与联系方式均有公开页面证据"
                                    if matched
                                    else "公开页面证据未达到有效线索标准"
                                ),
                            }
                        },
                    }
                )
            task["leads"] = verified
            self.store.save_leads(task_id, verified)
            task["verification_state"] = "completed"
            task["activity"].append(
                self._activity("verification_completed", outcome="completed")
            )
            task["updated_at"] = _now()
            self.store.save(task)
            return
        indexed_groups: dict[tuple[str, str], list[int]] = {}
        for index, lead in enumerate(task["leads"]):
            key = (lead.get("city") or "", lead.get("matched_keyword") or "")
            indexed_groups.setdefault(key, []).append(index)
        for (city, keyword), indexes in indexed_groups.items():
            group = [task["leads"][index] for index in indexes]
            verdicts_by_model: dict[str, list[dict[str, Any]]] = {}
            for model_id in task.get("models", []):
                try:
                    verdicts_by_model[model_id] = self.verifiers[model_id].verify(
                        group,
                        request_text=task["request_text"],
                        city=city,
                        keyword=keyword,
                    )
                except (KeyError, ModelVerificationError, TypeError, ValueError) as exc:
                    task["verification_errors"] += 1
                    task["activity"].append(
                        self._activity(
                            "verify_unit",
                            city=city,
                            keyword=keyword,
                            provider=model_id,
                            outcome="error",
                            detail=str(exc),
                        )
                    )
            if verdicts_by_model:
                verified = apply_verification_consensus(group, verdicts_by_model)
            else:
                verified = [
                    {
                        **lead,
                        "verification_status": "verification_failed",
                        "verification_models": [],
                        "verification_detail": {},
                        "confidence_level": "待验证",
                    }
                    for lead in group
                ]
            for index, lead in zip(indexes, verified, strict=True):
                task["leads"][index] = lead
            self.store.save_leads(task_id, verified)
            task["updated_at"] = _now()
            self.store.save(task)
        task["verification_state"] = (
            "completed_with_errors" if task["verification_errors"] else "completed"
        )
        task["activity"].append(
            self._activity("verification_completed", outcome=task["verification_state"])
        )
        task["updated_at"] = _now()
        self.store.save(task)

    def run_task(self, task_id: str) -> None:
        self._run_scheduled_task(task_id)

    def _run_scheduled_task(self, task_id: str) -> None:
        with self._task_operation_lock(task_id):
            task = self.store.load(task_id, include_leads=False)
            uses_web = bool(task.get("use_doubao_web_chat"))
            if uses_web:
                self._doubao_task_lease.acquire(
                    task_id,
                    lambda: self._control_checkpoint(task_id),
                )
            retain_web_lease = False
            try:
                retain_web_lease = self._run_scheduled_task_locked(task_id)
            finally:
                if uses_web and not retain_web_lease:
                    self._doubao_task_lease.release(task_id)

    def _run_scheduled_task_locked(self, task_id: str) -> bool:
        if not self._claim_task(task_id):
            return False
        try:
            self._control_checkpoint(task_id)
            self._run_task_serialized(task_id)
            self._control_checkpoint(task_id)
        except TaskPaused:
            self._mark_paused(task_id)
        except TaskWaitingForLogin:
            return True
        except TaskTerminated:
            self._mark_terminated(task_id)
        except Exception as exc:  # noqa: BLE001 - task runners must record unexpected provider failures
            detail = f"unexpected {type(exc).__name__}"[:160]
            if isinstance(exc, DoubaoWebResponseError):
                frames = traceback.extract_tb(exc.__traceback__)
                origin = ""
                if frames:
                    origin = f" at {Path(frames[-1].filename).name}:{frames[-1].lineno}"
                detail = f"{detail}{origin}: {exc}"[:160]
            if type(exc).__name__ == "TargetClosedError":
                detail = f"{detail}: {exc}"[:160]

            def mark_unexpected_error(task: dict[str, Any]) -> None:
                task["status"] = "completed_with_errors"
                task["completed_at"] = task.get("completed_at") or _now()
                task["updated_at"] = _now()
                task.setdefault("activity", []).append(
                    self._activity(
                        "run_task",
                        outcome="error",
                        detail=detail,
                    )
                )

            self.store.update(task_id, mark_unexpected_error)
        else:
            if not self.store.load(task_id, include_leads=False).get(
                "continuous_mode"
            ):
                self.store.update(
                    task_id,
                    lambda task: task.update(
                        completed_at=task.get("completed_at") or _now(),
                        updated_at=_now(),
                    ),
                )
        return False

    def _claim_task(self, task_id: str) -> bool:
        claimed = False

        def claim(task: dict[str, Any]) -> None:
            nonlocal claimed
            if task.get("terminate_requested") or task.get("status") == "terminated":
                task["status"] = "terminated"
                task["terminated_at"] = task.get("terminated_at") or _now()
                task["updated_at"] = _now()
                return
            if task.get("pause_requested") or task.get("status") == "paused":
                task["status"] = "paused"
                task["paused_at"] = task.get("paused_at") or _now()
                task["updated_at"] = _now()
                return
            if task.get("status") not in {"queued", "running"}:
                return
            claimed = True
            if task["status"] == "queued":
                task["status"] = "running"
                task["started_at"] = task.get("started_at") or _now()
                task["updated_at"] = _now()

        self.store.update(task_id, claim)
        return claimed

    def _control_checkpoint(self, task_id: str) -> None:
        task = self.store.load(task_id, include_leads=False)
        if task.get("terminate_requested"):
            raise TaskTerminated(task_id)
        if task.get("pause_requested"):
            raise TaskPaused(task_id)

    def _save_progress(self, task: dict[str, Any]) -> None:
        snapshot = dict(task)

        def merge(latest: dict[str, Any]) -> None:
            control = {
                key: latest.get(key)
                for key in (
                    "status",
                    "pause_requested",
                    "terminate_requested",
                    "paused_at",
                    "terminated_at",
                )
            }
            latest_by_key = {
                _delivery_identity(lead): lead for lead in latest.get("leads", [])
            }
            for incoming in snapshot.get("leads", []):
                current = latest_by_key.get(_delivery_identity(incoming))
                if current is not None:
                    _merge_delivery_state(current, incoming)
            latest.clear()
            latest.update(snapshot)
            if control["terminate_requested"]:
                latest["terminate_requested"] = True
                latest["status"] = control["status"]
                latest["terminated_at"] = control["terminated_at"]
            elif control["pause_requested"]:
                latest["pause_requested"] = True
                latest["status"] = control["status"]
                latest["paused_at"] = control["paused_at"]

        saved = self.store.update(task["id"], merge)
        task.clear()
        task.update(saved)

    def _mark_paused(self, task_id: str) -> None:
        def pause(task: dict[str, Any]) -> None:
            task["status"] = "paused"
            task["paused_at"] = task.get("paused_at") or _now()
            task["updated_at"] = _now()

        self.store.update(task_id, pause)

    def _mark_terminated(self, task_id: str) -> None:
        def terminate(task: dict[str, Any]) -> None:
            task["status"] = "terminated"
            task["terminated_at"] = task.get("terminated_at") or _now()
            task["updated_at"] = _now()

        self.store.update(task_id, terminate)

    def _run_task_serialized(self, task_id: str) -> None:
        task = self.store.load(task_id)
        self._deliver_pending_leads(task)
        if task.get("use_doubao_web_chat"):
            if task.get("continuous_mode"):
                self._run_continuous_task(task_id)
            else:
                self._run_two_stage_task(task)
            return
        if any(
            callable(getattr(self.verifiers.get(model_id), "discover_companies", None))
            for model_id in task.get("models", [])
        ):
            if task.get("continuous_mode"):
                self._run_continuous_task(task_id)
            else:
                self._run_two_stage_task(task)
            return
        completed_keys = set(task["completed_unit_keys"])
        for city in task["cities"]:
            for keyword in task["keywords"]:
                for provider_id in task["providers"]:
                    unit_key = json.dumps([city, keyword, provider_id], ensure_ascii=False)
                    if unit_key in completed_keys:
                        continue
                    outcome = "completed"
                    detail = None
                    leads_before = len(task["leads"])
                    normalized: list[dict[str, Any]] = []
                    try:
                        self._control_checkpoint(task_id)
                        provider = self.providers[provider_id]
                        raw_leads = provider.collect(city=city, keyword=keyword, request_text=task["request_text"])
                        normalized.extend(
                            normalize_lead(raw, city=city, provider=provider_id, keyword=keyword)
                            for raw in raw_leads
                        )
                    except (KeyError, ProviderError, TypeError, ValueError) as exc:
                        outcome = "error"
                        detail = str(exc)
                        task["unit_errors"] += 1
                        self._save_progress(task)
                        self._control_checkpoint(task_id)
                    else:
                        self._persist_legacy_candidates(task, city, normalized)
                        self._control_checkpoint(task_id)
                    for model_id in task.get("models", []):
                        verifier = self.verifiers.get(model_id)
                        discover = getattr(verifier, "discover", None)
                        if not callable(discover):
                            continue
                        discovered: list[dict[str, Any]] | None = None
                        discovery_error: Exception | None = None
                        for _ in range(2):
                            try:
                                self._control_checkpoint(task_id)
                                discovered = discover(
                                    request_text=task["request_text"],
                                    city=city,
                                    keyword=keyword,
                                )
                                break
                            except (ModelVerificationError, TypeError, ValueError) as exc:
                                discovery_error = exc
                                self._control_checkpoint(task_id)
                        if discovered is None:
                            task["activity"].append(
                                self._activity(
                                    "discover_unit",
                                    city=city,
                                    keyword=keyword,
                                    provider=model_id,
                                    outcome="error",
                                    detail=str(discovery_error),
                                )
                            )
                            self._save_progress(task)
                            self._control_checkpoint(task_id)
                            continue
                        contact_candidates = sum(
                            bool(raw.get("mobile") or raw.get("email") or raw.get("wechat"))
                            for raw in discovered
                        )
                        task["activity"].append(
                            self._activity(
                                "discover_unit",
                                city=city,
                                keyword=keyword,
                                provider=model_id,
                                outcome="completed",
                                detail=(
                                    f"returned {len(discovered)} candidates, "
                                    f"{contact_candidates} with contacts"
                                ),
                            )
                        )
                        normalized.extend(
                            normalize_lead(raw, city=city, provider=model_id, keyword=keyword)
                            for raw in discovered
                        )
                        self._persist_legacy_candidates(task, city, normalized)
                        self._control_checkpoint(task_id)
                    city_leads = [
                        lead for lead in task["leads"] if lead["city"] == city
                    ]
                    for lead in city_leads:
                        self._deliver_lead(task, lead)
                    if outcome == "completed" and len(task["leads"]) == leads_before:
                        outcome = "empty"
                        detail = "no new qualified public-web contacts"
                        task["empty_units"] += 1
                    completed_keys.add(unit_key)
                    task["completed_unit_keys"] = list(completed_keys)
                    task["completed_units"] = len(completed_keys)
                    task["lead_count"] = len(task["leads"])
                    task["activity"].append(
                        self._activity(
                            "collect_unit",
                            city=city,
                            keyword=keyword,
                            provider=provider_id,
                            outcome=outcome,
                            detail=detail,
                        )
                    )
                    self._update_city_progress(task, city)
                    task["updated_at"] = _now()
                    self._save_progress(task)
                    self._control_checkpoint(task_id)
        task["status"] = (
            "completed_with_errors"
            if task["unit_errors"] or not task["leads"]
            else "completed"
        )
        task["updated_at"] = _now()
        self._save_progress(task)

    def _persist_legacy_candidates(
        self,
        task: dict[str, Any],
        city: str,
        candidates: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        with self._candidate_reservation_lock:
            return self._persist_legacy_candidates_unlocked(task, city, candidates)

    def _persist_legacy_candidates_unlocked(
        self,
        task: dict[str, Any],
        city: str,
        candidates: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        required_contacts = task.get("required_contacts") or ["mobile"]
        model_channels = set(task.get("models", []))
        qualified = [
            lead
            for lead in candidates
            if lead["company_name"]
            and (
                lead["discovery_channel"] in model_channels
                or (
                    (lead["mobile"] or lead["email"] or lead["wechat"])
                    and _matches_contacts(
                        lead,
                        required_contacts,
                        task.get("contact_match", "all"),
                    )
                )
            )
        ]
        city_key = city.strip().casefold()
        reserved_names = self._reserved_candidate_keys(task_id=task["id"], city=city)
        is_company_delivered = getattr(self.lead_delivery, "is_company_delivered", None)
        eligible: list[dict[str, Any]] = []
        for lead in qualified:
            lead_city = str(lead.get("city") or city).strip()
            if lead_city.casefold() != city_key:
                continue
            normalized_name = normalize_company_name(lead.get("company_name"))
            if not normalized_name:
                continue
            key = (city_key, normalized_name.casefold())
            if key in reserved_names:
                continue
            if callable(is_company_delivered) and is_company_delivered(
                city, normalized_name
            ):
                continue
            eligible.append({**lead, "city": city, "company_name": normalized_name})
            reserved_names.add(key)
        task["leads"] = deduplicate_leads([*task["leads"], *eligible])
        if limit := task.get("limit_per_city"):
            city_leads = [lead for lead in task["leads"] if lead["city"] == city][
                :limit
            ]
            task["leads"] = [
                lead for lead in task["leads"] if lead["city"] != city
            ] + city_leads
        city_leads = [lead for lead in task["leads"] if lead["city"] == city]
        self.store.reconcile_leads(task["id"], city, city_leads)
        task["lead_count"] = len(task["leads"])
        task["updated_at"] = _now()
        self._save_progress(task)
        return city_leads

    def _run_continuous_task(self, task_id: str) -> None:
        while True:
            self._control_checkpoint(task_id)
            task = self.store.load(task_id)
            self._prune_historical_candidate_duplicates(task)
            target = int(task.get("valid_lead_target") or 0)
            if target and target_contact_count(task) >= target:
                task["round_phase"] = "completed"
                task["collection_phase"] = "completed"
                task["status"] = "completed"
                task["completed_at"] = _now()
                task["next_round_at"] = None
                task["updated_at"] = _now()
                self._save_progress(task)
                return
            if task.get("round_phase") == "cooldown" and task.get("next_round_at"):
                self._wait_for_next_round(task_id)
                self._prepare_next_round(task_id)
                task = self.store.load(task_id)
            if not task.get("round_started_at"):
                task["round_started_at"] = self._now().isoformat()
            task["round_phase"] = "discovering"
            task["status"] = "running"
            task["round_metrics"] = new_round_metrics()
            before_count = len(task.get("leads", []))
            original_keywords = list(task.get("keywords", []))
            task["keywords"] = rotate_keywords(
                original_keywords, int(task.get("round_number") or 1)
            )
            self._save_progress(task)

            enrich_existing_only = candidate_pool_is_sufficient(
                len(task.get("leads", [])),
                int(task.get("candidate_target") or ROUND_CANDIDATE_LIMIT),
            )
            if enrich_existing_only:
                task["round_phase"] = "enriching"
                task["collection_phase"] = "enriching"
                task["activity"].append(
                    self._activity(
                        "enrichment_only_round",
                        outcome="accepted",
                        detail="候选池已足够，本轮只补全已有候选公司的联系方式",
                    )
                )
                self._save_progress(task)

            self._run_two_stage_task(
                task,
                finalize=False,
                round_limit=ROUND_CANDIDATE_LIMIT,
                skip_discovery=enrich_existing_only,
            )
            task["keywords"] = original_keywords
            after_count = len(task.get("leads", []))
            task["round_metrics"]["candidates"] = max(0, after_count - before_count)
            task["round_metrics"]["new_companies"] = max(0, after_count - before_count)
            task["round_phase"] = "verifying"
            task["verification_state"] = "running"
            self._save_progress(task)
            self._verify_task_locked(task_id)

            task = self.store.load(task_id)
            task["round_phase"] = "delivering"
            matches = sum(
                lead.get("verification_status") in MATCHED_VERIFICATION_STATUSES
                for lead in task.get("leads", [])
            )
            task["round_metrics"]["verification_matches"] = matches
            task["round_metrics"]["verification_rejects"] = max(
                0, len(task.get("leads", [])) - matches
            )
            self._save_progress(task)
            self._deliver_verified_pending_leads(task)

            task = self.store.load(task_id)
            task["round_metrics"]["erp_succeeded"] = sum(
                lead.get("delivery_status") == "succeeded"
                for lead in task.get("leads", [])
            )
            task["round_metrics"]["erp_failed"] = sum(
                lead.get("delivery_status") == "failed"
                for lead in task.get("leads", [])
            )
            target = int(task.get("valid_lead_target") or 0)
            if target and target_contact_count(task) >= target:
                task["round_phase"] = "completed"
                task["collection_phase"] = "completed"
                task["status"] = "completed"
                task["completed_at"] = _now()
                task["next_round_at"] = None
                task["updated_at"] = _now()
                self._save_progress(task)
                return
            task["round_phase"] = "cooldown"
            cooldown_seconds = (
                TARGET_ROUND_COOLDOWN_SECONDS
                if task.get("valid_lead_target")
                else ROUND_COOLDOWN_SECONDS
            )
            task["next_round_at"] = (
                self._now() + timedelta(seconds=cooldown_seconds)
            ).isoformat()
            task["status"] = "running"
            task["updated_at"] = _now()
            self._save_progress(task)
            self._wait_for_next_round(task_id)
            self._prepare_next_round(task_id)

    def _wait_for_next_round(self, task_id: str) -> None:
        while True:
            self._control_checkpoint(task_id)
            task = self.store.load(task_id, include_leads=False)
            target = datetime.fromisoformat(str(task["next_round_at"]))
            remaining = (target - self._now()).total_seconds()
            if remaining <= 0:
                return
            self._sleep(min(1.0, remaining))

    def _prepare_next_round(self, task_id: str) -> None:
        def prepare(task: dict[str, Any]) -> None:
            task["round_number"] = int(task.get("round_number") or 1) + 1
            task["round_phase"] = "discovering"
            task["round_started_at"] = self._now().isoformat()
            task["next_round_at"] = None
            task["round_metrics"] = new_round_metrics()
            task["round_checkpoint"] = {}
            task["completed_city_discovery"] = []
            task["completed_units"] = 0
            task["collection_phase"] = "discovering"
            task["verification_state"] = "not_started"
            web_checkpoint = task.setdefault("web_chat_checkpoint", {})
            web_checkpoint.update(
                city=None,
                batch_number=0,
                company_index=0,
            )
            web_checkpoint.pop("round_start_candidate_count", None)
            task["updated_at"] = _now()

        self.store.update(task_id, prepare)

    def _run_two_stage_task(
        self,
        task: dict[str, Any],
        *,
        finalize: bool = True,
        round_limit: int | None = None,
        skip_discovery: bool = False,
    ) -> None:
        requested_limit = int(task.get("limit_per_city") or 100)
        per_city_limit = max(
            1,
            (
                round_limit
                if round_limit is not None and task.get("continuous_mode")
                else min(round_limit or 100, requested_limit)
            ),
        )
        task["total_units"] = len(task["cities"])
        task["candidate_target"] = len(task["cities"]) * per_city_limit
        task.setdefault("completed_city_discovery", [])
        task.setdefault("candidate_count", len(task.get("leads", [])))
        task.setdefault("enrichment_total", len(task.get("leads", [])))
        task.setdefault("enrichment_completed", 0)
        completed_cities = set(task["completed_city_discovery"])

        for city in task["cities"]:
            if city not in completed_cities:
                if not skip_discovery:
                    self._discover_city_candidates(task, city, per_city_limit)
                completed_cities.add(city)
                task["completed_city_discovery"] = list(completed_cities)
                task["updated_at"] = _now()
                self._save_progress(task)
            # Verify and enrich in bounded batches so the first confirmed
            # companies can receive a Doubao contact lookup without waiting
            # for every candidate in the round to finish verification.
            if task.get("use_doubao_web_chat"):
                # The Doubao discovery response already carries a source URL and
                # business quote.  Let contact evidence finish the company check
                # instead of spending a second search pass on every candidate.
                self._enrich_city_candidates(task, city, allow_unverified=True)
            elif callable(getattr(self.company_name_verifier, "verify_company_name", None)):
                while self._verify_city_company_names(
                    task,
                    city,
                    max_candidates=COMPANY_VERIFICATION_BATCH_SIZE,
                ):
                    self._enrich_city_candidates(task, city, allow_unverified=False)
                self._enrich_city_candidates(task, city, allow_unverified=True)
            else:
                self._enrich_city_candidates(task, city)
            task["completed_units"] = len(
                {
                    lead["city"]
                    for lead in task["leads"]
                    if lead.get("enrichment_state") in TERMINAL_ENRICHMENT_STATES
                }
            )
            progress = task["city_progress"].setdefault(city, {"city": city})
            city_leads = [lead for lead in task["leads"] if lead["city"] == city]
            progress.update(
                {
                    "status": "completed",
                    "total_units": len(city_leads),
                    "completed_units": sum(
                        lead.get("enrichment_state") in TERMINAL_ENRICHMENT_STATES
                        for lead in city_leads
                    ),
                    "lead_count": len(city_leads),
                }
            )
            task["updated_at"] = _now()
            self._save_progress(task)

        task["collection_phase"] = "completed"
        task["lead_count"] = len(task["leads"])
        if finalize:
            task["status"] = (
                "completed_with_errors"
                if task["unit_errors"] or not task["leads"]
                else "completed"
            )
        else:
            task["status"] = "running"
        task["updated_at"] = _now()
        self._save_progress(task)

    def _discover_city_candidates(
        self,
        task: dict[str, Any],
        city: str,
        limit: int,
    ) -> None:
        if task.get("use_doubao_web_chat") and task.get("web_chat_discovery"):
            city_count_before = sum(
                lead.get("city") == city for lead in task.get("leads", [])
            )
            if not task.get("continuous_mode") and city_count_before >= limit:
                return
            # The user's selected source is the persistent Doubao chat. Query
            # it first so a slow public search cannot make the task appear
            # stalled; public search is only used to fill the remaining quota.
            doubao_limit = (
                limit
                if task.get("continuous_mode")
                else max(0, limit - city_count_before)
            )
            public_fallback_used = False
            if doubao_limit:
                public_fallback_used = self._discover_city_candidates_web(
                    task, city, doubao_limit
                )
            if public_fallback_used:
                return
            city_count_after_doubao = sum(
                lead.get("city") == city for lead in task.get("leads", [])
            )
            if task.get("continuous_mode"):
                remaining = max(
                    0, limit - (city_count_after_doubao - city_count_before)
                )
            else:
                remaining = max(0, limit - city_count_after_doubao)
            if remaining:
                self._discover_public_candidates(
                    task,
                    city,
                    remaining if task.get("continuous_mode") else limit,
                    round_start_count=city_count_after_doubao,
                    max_keywords=PUBLIC_FALLBACK_KEYWORDS_PER_ROUND,
                )
            return
        task["collection_phase"] = "discovering"
        raw_candidates: list[dict[str, Any]] = []
        successful_models = 0
        excluded_names = [
            str(lead.get("company_name") or "").strip()
            for lead in task.get("leads", [])
            if lead.get("city") == city and lead.get("company_name")
        ][-100:]
        query_groups = (
            discovery_query_groups(
                list(task["keywords"]),
                round_number=int(task.get("round_number") or 1),
            )
            if task.get("continuous_mode")
            else [list(task["keywords"])]
        )
        for model_id in task.get("models", []):
            verifier = self.verifiers.get(model_id)
            discover = getattr(verifier, "discover_companies", None)
            if not callable(discover):
                continue
            for query_keywords in query_groups:
                current_exclusions = list(
                    dict.fromkeys(
                        [
                            *excluded_names,
                            *(
                                str(item.get("company_name") or "").strip()
                                for item in raw_candidates
                                if item.get("company_name")
                            ),
                        ]
                    )
                )[-100:]
                discovery_request = (
                    task["request_text"]
                    + "\n本次搜索方向："
                    + "、".join(query_keywords)
                    + "。请按城市各行政区和公开项目案例继续寻找不同公司。"
                )
                if current_exclusions:
                    discovery_request += (
                        "\n不要重复以下已处理公司：" + "、".join(current_exclusions)
                    )
                discovered: list[dict[str, Any]] | None = None
                last_error: Exception | None = None
                for _ in range(2):
                    try:
                        self._control_checkpoint(task["id"])
                        discovered = discover(
                            request_text=discovery_request,
                            city=city,
                            keywords=query_keywords,
                            limit=max(10, min(30, limit)),
                        )
                        break
                    except ModelAccountError as exc:
                        detail = str(exc)[:500]

                        def pause_for_account_error(
                            latest: dict[str, Any],
                            error_detail: str = detail,
                            provider_id: str = model_id,
                            city_name: str = city,
                        ) -> None:
                            latest["pause_requested"] = True
                            latest["pause_reason"] = "model_account_blocked"
                            latest["last_blocking_error"] = error_detail
                            latest["updated_at"] = _now()
                            latest.setdefault("activity", []).append(
                                self._activity(
                                    "discover_companies",
                                    city=city_name,
                                    provider=provider_id,
                                    outcome="blocked",
                                    detail=error_detail,
                                )
                            )

                        self.store.update(task["id"], pause_for_account_error)
                        raise TaskPaused(task["id"]) from exc
                    except (ModelVerificationError, TypeError, ValueError) as exc:
                        last_error = exc
                        self._control_checkpoint(task["id"])
                if discovered is None:
                    task["activity"].append(
                        self._activity(
                            "discover_companies",
                            city=city,
                            provider=model_id,
                            outcome="error",
                            detail=str(last_error),
                        )
                    )
                    continue
                successful_models += 1
                raw_candidates.extend(
                    {
                        **candidate,
                        "city": city,
                        "discovery_channel": model_id,
                    }
                    for candidate in discovered
                )
                task["activity"].append(
                    self._activity(
                        "discover_companies",
                        city=city,
                        provider=model_id,
                        outcome="completed",
                        detail=f"returned {len(discovered)} candidates",
                    )
                )
                self._persist_discovered_candidates(task, city, raw_candidates, limit)
                self._control_checkpoint(task["id"])

        for provider_id in task.get("providers", []):
            provider = self.providers.get(provider_id)
            if provider is None:
                continue
            for keyword in task["keywords"]:
                try:
                    self._control_checkpoint(task["id"])
                    public_leads = provider.collect(
                        city=city,
                        keyword=keyword,
                        request_text=task["request_text"],
                    )
                except (ProviderError, TypeError, ValueError) as exc:
                    task["activity"].append(
                        self._activity(
                            "discover_public_candidates",
                            city=city,
                            keyword=keyword,
                            provider=provider_id,
                            outcome="error",
                            detail=str(exc),
                        )
                    )
                    self._control_checkpoint(task["id"])
                    continue
                raw_candidates.extend(
                    {
                        **lead,
                        "city": city,
                        "matched_keyword": lead.get("matched_keyword") or keyword,
                        "discovery_channel": provider_id,
                        "source_confidence": lead.get("source_confidence") or 60,
                    }
                    for lead in public_leads
                )

                self._persist_discovered_candidates(
                    task,
                    city,
                    raw_candidates,
                    limit,
                )
                self._control_checkpoint(task["id"])

        if not raw_candidates and not successful_models:
            task["unit_errors"] += 1
        self._persist_discovered_candidates(task, city, raw_candidates, limit)

    def _discover_public_candidates(
        self,
        task: dict[str, Any],
        city: str,
        limit: int,
        *,
        round_start_count: int | None = None,
        max_keywords: int | None = None,
    ) -> None:
        raw_candidates: list[dict[str, Any]] = []
        if round_start_count is None:
            round_start_count = sum(
                lead.get("city") == city for lead in task.get("leads", [])
            )
        keywords = list(task.get("keywords") or [])
        if max_keywords is not None:
            keywords = keywords[: max(0, int(max_keywords))]
        for provider_id in task.get("providers", []):
            provider = self.providers.get(provider_id)
            if provider is None:
                continue
            for keyword in keywords:
                try:
                    self._control_checkpoint(task["id"])
                    public_leads = provider.collect(
                        city=city,
                        keyword=keyword,
                        request_text=task["request_text"],
                    )
                except (ProviderError, TypeError, ValueError) as exc:
                    task["activity"].append(
                        self._activity(
                            "discover_public_candidates",
                            city=city,
                            keyword=keyword,
                            provider=provider_id,
                            outcome="error",
                            detail=str(exc),
                        )
                    )
                    self._control_checkpoint(task["id"])
                    continue
                raw_candidates.extend(
                    {
                        **lead,
                        "city": city,
                        "matched_keyword": lead.get("matched_keyword") or keyword,
                        "discovery_channel": provider_id,
                        "source_confidence": lead.get("source_confidence") or 60,
                    }
                    for lead in public_leads
                )
                self._persist_discovered_candidates(task, city, raw_candidates, limit)
                city_count_after = sum(
                    lead.get("city") == city for lead in task.get("leads", [])
                )
                target_reached = (
                    city_count_after - round_start_count >= limit
                    if task.get("continuous_mode")
                    else city_count_after >= limit
                )
                if target_reached:
                    return
                self._control_checkpoint(task["id"])

        if max_keywords is not None and len(task.get("keywords") or []) > len(keywords):
            task.setdefault("activity", []).append(
                self._activity(
                    "public_discovery_budget",
                    city=city,
                    provider="web_search",
                    outcome="deferred",
                    detail=(
                        f"本轮公开网页只查询 {len(keywords)} 个关键词，"
                        f"剩余 {len(task.get('keywords') or []) - len(keywords)} 个关键词留待下一轮"
                    ),
                )
            )
            task["updated_at"] = _now()
            self._save_progress(task)

    def _discover_city_candidates_web(
        self,
        task: dict[str, Any],
        city: str,
        limit: int,
    ) -> bool:
        gateway = self.doubao_web_gateway
        if gateway is None:
            raise InvalidTaskStateError("豆包网页采集未启用")
        task["collection_phase"] = "discovering"
        task["web_chat_status"] = "working"
        # Replace an old fallback notice when a new discovery pass actually
        # starts; only the current pass may report a blocking web error.
        task["web_chat_error"] = None
        retry_after = str(task.get("web_chat_discovery_retry_after") or "").strip()
        if retry_after:
            try:
                backoff_active = datetime.fromisoformat(retry_after) > self._now()
            except ValueError:
                backoff_active = False
            if backoff_active:
                task["web_chat_error"] = (
                    "豆包网页发现近期连续超时，暂时跳过本轮豆包查询并使用公开网页"
                )
                task.setdefault("activity", []).append(
                    self._activity(
                        "doubao_web_discovery_backoff",
                        city=city,
                        provider="web_search",
                        outcome="fallback",
                        detail=f"退避至 {retry_after}，避免重复等待豆包网页超时",
                    )
                )
                current_count = sum(
                    lead.get("city") == city for lead in task.get("leads", [])
                )
                if limit:
                    self._discover_public_candidates(
                        task,
                        city,
                        limit,
                        round_start_count=current_count,
                        max_keywords=PUBLIC_FALLBACK_KEYWORDS_PER_ROUND,
                    )
                task["web_chat_status"] = "ready"
                task["web_chat_current_company"] = None
                self._save_progress(task)
                return True
        raw_candidates: list[dict[str, Any]] = []
        checkpoint = task.setdefault("web_chat_checkpoint", {})
        start_batch = (
            int(checkpoint.get("batch_number") or 0)
            if checkpoint.get("city") == city
            else 0
        )
        base_max_batches = max(1, (limit + 4) // 5)
        # A short or duplicate response can mean that Doubao found fewer new
        # companies than requested. Allow at most two finite continuation
        # batches, carrying that budget through pause/restart checkpoints; a
        # response/parsing error disables further expansion for this round.
        max_batches = base_max_batches + (2 if start_batch == 0 else 0)
        if checkpoint.get("city") == city:
            saved_max_batches = int(checkpoint.get("max_batches") or 0)
            if saved_max_batches >= base_max_batches:
                max_batches = saved_max_batches
            elif start_batch > 0:
                # Checkpoints written before max_batches was introduced only
                # recorded the last completed batch. Keep one continuation
                # batch available so a resumed task cannot finish early.
                max_batches = max(base_max_batches, start_batch + 1)
        short_batch_extensions = 0
        response_error_seen = False
        if checkpoint.get("city") == city:
            short_batch_extensions = int(checkpoint.get("short_batch_extensions") or 0)
            response_error_seen = bool(checkpoint.get("response_error_seen"))
        current_city_count = sum(
            lead.get("city") == city for lead in task.get("leads", [])
        )
        round_start_count = int(
            checkpoint.get("round_start_candidate_count", current_city_count)
            if checkpoint.get("city") == city
            else current_city_count
        )
        batch_number = start_batch
        while batch_number < max_batches:
            continuation = (
                f"\n\n这是第 {batch_number + 1} 批。"
                "请只返回本批符合条件且尚未出现过的新公司；系统会自动去重，"
                "不要在回答中列出排除名单，也不要用重复公司凑数。"
            )
            checkpoint.update(
                city=city,
                batch_number=batch_number,
                company_index=0,
                processed_normalized_names=checkpoint.get("processed_normalized_names", []),
                round_start_candidate_count=round_start_count,
                max_batches=max_batches,
                short_batch_extensions=short_batch_extensions,
                response_error_seen=response_error_seen,
            )
            self._save_progress(task)
            discovered = None
            response_error: DoubaoWebResponseError | None = None
            web_failure: DoubaoWebTimeout | DoubaoWebUnavailable | None = None
            for _attempt in range(3):
                try:
                    discovered = gateway.discover_companies(
                        city=city,
                        keywords=list(task.get("keywords") or []),
                        request_text=task["request_text"] + continuation,
                    )
                    break
                except (DoubaoLoginRequired, DoubaoChallengeRequired) as exc:
                    self._wait_for_doubao_login(task, exc, company_name=None)
                except (DoubaoWebTimeout, DoubaoWebUnavailable) as exc:
                    # A slow or disconnected chat page must not terminate the
                    # collection task. Stop spending time on this batch and
                    # continue with the original public-web discovery path.
                    web_failure = exc
                    break
                except DoubaoWebResponseError as exc:
                    response_error = exc
                    self._control_checkpoint(task["id"])
            if web_failure is not None:
                task["web_chat_error"] = (
                    f"豆包网页查询未完成，已回退原有公开网页逻辑：{web_failure}"
                )
                task["web_chat_discovery_failures"] = int(
                    task.get("web_chat_discovery_failures") or 0
                ) + 1
                task["web_chat_discovery_retry_after"] = (
                    self._now() + timedelta(seconds=_DOUBAO_DISCOVERY_BACKOFF_SECONDS)
                ).isoformat()
                task.setdefault("activity", []).append(
                    self._activity(
                        "doubao_web_discovery_fallback",
                        city=city,
                        provider="web_search",
                        outcome="fallback",
                        detail=str(web_failure)[:160],
                    )
                )
                current_count = sum(
                    lead.get("city") == city for lead in task.get("leads", [])
                )
                collected_this_round = (
                    current_count - round_start_count
                    if task.get("continuous_mode")
                    else current_count
                )
                fallback_limit = max(0, limit - collected_this_round)
                if fallback_limit:
                    self._discover_public_candidates(
                        task,
                        city,
                        (
                            fallback_limit
                            if task.get("continuous_mode")
                            else round_start_count + limit
                        ),
                        round_start_count=current_count,
                        max_keywords=PUBLIC_FALLBACK_KEYWORDS_PER_ROUND,
                    )
                checkpoint["batch_number"] = max_batches
                task["web_chat_status"] = "ready"
                task["web_chat_current_company"] = None
                self._save_progress(task)
                return True
            if discovered is None:
                checkpoint["batch_number"] = batch_number + 1
                detail = str(response_error or "豆包返回无法解析")
                response_error_seen = True
                checkpoint["response_error_seen"] = True
                task["web_chat_error"] = detail
                task.setdefault("activity", []).append(
                    self._activity(
                        "doubao_web_discovery_batch",
                        city=city,
                        provider="doubao_web",
                        outcome="error",
                        detail=f"第 {batch_number + 1} 批重试 3 次后跳过: {detail}"[:160],
                    )
                )
                self._save_progress(task)
                if batch_number + 1 >= base_max_batches:
                    break
                batch_number += 1
                continue
            discovered = discovered[:5]
            task["web_chat_error"] = None
            task["web_chat_discovery_failures"] = 0
            task["web_chat_discovery_retry_after"] = None
            raw_candidates.extend(
                {
                    "company_name": candidate.company_name,
                    "city": city,
                    "matched_keyword": candidate.matched_keyword,
                    "source_url": candidate.source_urls[0] if candidate.source_urls else None,
                    "company_name_evidence_urls": list(candidate.source_urls),
                    "reason": "；".join(candidate.evidence_quotes),
                    "discovery_channel": "doubao_web",
                    "source_confidence": 70,
                }
                for candidate in discovered
            )
            city_count_before_batch = sum(
                lead.get("city") == city for lead in task.get("leads", [])
            )
            self._persist_discovered_candidates(
                task,
                city,
                raw_candidates,
                limit,
                max_new=limit,
            )
            checkpoint["batch_number"] = batch_number + 1
            self._save_progress(task)
            self._control_checkpoint(task["id"])
            city_count_after = sum(
                lead.get("city") == city for lead in task.get("leads", [])
            )
            accepted_this_batch = city_count_after - city_count_before_batch
            if not discovered or city_count_after - round_start_count >= limit:
                break
            if batch_number + 1 >= base_max_batches:
                if (
                    max_batches > base_max_batches
                    and not response_error_seen
                    and short_batch_extensions < max_batches - base_max_batches
                    and (len(discovered) < 5 or accepted_this_batch <= 0)
                ):
                    short_batch_extensions += 1
                else:
                    break
            checkpoint.update(
                max_batches=max_batches,
                short_batch_extensions=short_batch_extensions,
                response_error_seen=response_error_seen,
            )
            self._save_progress(task)
            batch_number += 1
        task["web_chat_status"] = "ready"
        return False

    def _wait_for_doubao_login(
        self,
        task: dict[str, Any],
        error: Exception,
        *,
        company_name: str | None,
    ) -> None:
        is_challenge = isinstance(error, DoubaoChallengeRequired)
        task["status"] = "waiting_for_login"
        task["web_chat_status"] = "challenge" if is_challenge else "waiting_for_login"
        task["pause_reason"] = "doubao_challenge" if is_challenge else "doubao_login"
        task["web_chat_error"] = str(error)
        task["web_chat_current_company"] = company_name
        task["updated_at"] = _now()
        self._save_progress(task)
        raise TaskWaitingForLogin(str(error)) from error

    def _persist_discovered_candidates(
        self,
        task: dict[str, Any],
        city: str,
        raw_candidates: list[dict[str, Any]],
        limit: int,
        *,
        max_new: int | None = None,
    ) -> None:
        with self._candidate_reservation_lock:
            self._persist_discovered_candidates_unlocked(
                task,
                city,
                raw_candidates,
                limit,
                max_new=max_new,
            )

    @staticmethod
    def _merge_discovered_candidate_into_lead(
        current: dict[str, Any],
        candidate: dict[str, Any],
        city: str,
    ) -> None:
        channel = str(candidate.get("discovery_channel") or "doubao_web")
        candidate_payload = dict(candidate)
        candidate_payload.setdefault("mobile", _candidate_mobile(candidate_payload))
        normalized = normalize_lead(
            candidate_payload,
            city=city,
            provider=channel,
            keyword=str(candidate_payload.get("matched_keyword") or ""),
        )
        for field in (
            "mobile",
            "email",
            "wechat",
            "contact_person_name",
            "contact_person_role",
            "address",
            "service_intro",
            "source_url",
            "contact_evidence_url",
            "contact_evidence_quote",
        ):
            if not current.get(field) and normalized.get(field):
                current[field] = normalized[field]
        for field in (
            "contact_evidence_urls",
            "contact_evidence_quotes",
            "company_name_evidence_urls",
        ):
            values: list[Any] = []
            for value in (current.get(field), normalized.get(field), candidate.get(field)):
                if isinstance(value, (list, tuple, set)):
                    values.extend(value)
                elif value:
                    values.append(value)
            if values:
                current[field] = list(dict.fromkeys(values))
        if not current.get("candidate_reason") and candidate.get("reason"):
            current["candidate_reason"] = str(candidate["reason"])
        candidate_confidence = int(candidate.get("source_confidence") or 0)
        current_confidence = int(current.get("source_confidence") or 0)
        if candidate_confidence > current_confidence:
            current["source_confidence"] = candidate_confidence
            current["confidence_score"] = candidate_confidence
            if candidate.get("matched_keyword"):
                current["matched_keyword"] = candidate["matched_keyword"]
            if candidate.get("discovery_channel"):
                current["discovery_channel"] = candidate["discovery_channel"]

    def _persist_discovered_candidates_unlocked(
        self,
        task: dict[str, Any],
        city: str,
        raw_candidates: list[dict[str, Any]],
        limit: int,
        *,
        max_new: int | None = None,
    ) -> None:
        # Reconcile any rows already persisted before considering this batch.
        # Older runs may contain formatting variants of the same company; if we
        # build the lookup first, those rows survive and get enriched twice.
        task["leads"] = deduplicate_leads(task.get("leads", []))
        candidates = deduplicate_company_candidates(raw_candidates)
        existing_by_key = {
            (
                str(lead.get("city") or city).strip().casefold(),
                (
                    normalize_company_name(lead.get("company_name"))
                    or str(lead.get("company_name") or "").strip()
                ).casefold(),
            ): lead
            for lead in task["leads"]
        }
        reserved_names = self._reserved_candidate_keys(task_id=task["id"], city=city)
        existing_city_count = sum(
            str(lead.get("city") or city).strip().casefold() == city.strip().casefold()
            for lead in task["leads"]
        )
        max_new_candidates = (
            max_new
            if max_new is not None
            else (
                limit
                if task.get("continuous_mode")
                else max(0, limit - existing_city_count)
            )
        )
        eligible_candidates: list[dict[str, Any]] = []
        is_company_delivered = getattr(self.lead_delivery, "is_company_delivered", None)
        for candidate in candidates:
            normalized_name = normalize_company_name(candidate.get("company_name"))
            if not normalized_name:
                continue
            key = (city.strip().casefold(), normalized_name.casefold())
            existing = existing_by_key.get(key)
            if existing is not None:
                self._merge_discovered_candidate_into_lead(existing, candidate, city)
                continue
            if max_new_candidates <= 0:
                break
            if key in reserved_names:
                continue
            if callable(is_company_delivered) and is_company_delivered(
                city, normalized_name
            ):
                continue
            eligible_candidates.append({**candidate, "company_name": normalized_name})
            reserved_names.add(key)
            if len(eligible_candidates) >= max_new_candidates:
                break

        new_leads: list[dict[str, Any]] = []
        for candidate in eligible_candidates:
            channel = str(candidate.get("discovery_channel") or task["models"][0])
            candidate_payload = dict(candidate)
            candidate_payload.setdefault(
                "mobile", _candidate_mobile(candidate_payload)
            )
            lead = normalize_lead(
                candidate_payload,
                city=city,
                provider=channel,
                keyword=str(candidate_payload.get("matched_keyword") or ""),
            )
            lead.update(
                {
                    "candidate_reason": str(candidate.get("reason") or ""),
                    "enrichment_state": "pending",
                    "verification_status": "model_discovered",
                    "company_name_evidence_urls": list(
                        candidate.get("company_name_evidence_urls") or []
                    ),
                }
            )
            candidate_name_status = str(
                candidate.get("company_name_verification_status") or "unverified"
            )
            if candidate_name_status in VERIFIED_COMPANY_NAME_STATUSES:
                lead.update(
                    company_name_verification_status=candidate_name_status,
                    company_name_evidence_urls=list(
                        candidate.get("company_name_evidence_urls") or []
                    ),
                    company_name_verification_reason=(
                        "豆包联网引用确认完整公司名称和所在城市"
                    ),
                    company_name_verified_at=_now(),
                    company_name_verification_attempts=1,
                )
            task["leads"].append(lead)
            new_leads.append(lead)

        # A contact can identify the same company even when two discovery
        # responses used slightly different names. Reconcile once more before
        # the task is checkpointed so duplicate rows never enter enrichment.
        task["leads"] = deduplicate_leads(task["leads"])
        for lead in task["leads"]:
            normalized_name = normalize_company_name(lead.get("company_name"))
            if normalized_name:
                lead["company_name"] = normalized_name

        if new_leads:
            self.store.save_leads(task["id"], new_leads)

        task["candidate_count"] = len(task["leads"])
        task["enrichment_total"] = len(task["leads"])
        task["enrichment_completed"] = sum(
            lead.get("enrichment_state") in TERMINAL_ENRICHMENT_STATES
            for lead in task["leads"]
        )
        task["lead_count"] = len(task["leads"])
        task["updated_at"] = _now()
        self._save_progress(task)

    def _verify_city_company_names(
        self,
        task: dict[str, Any],
        city: str,
        *,
        max_candidates: int | None = None,
    ) -> int:
        verify = getattr(self.company_name_verifier, "verify_company_name", None)
        if not callable(verify):
            return 0
        task["collection_phase"] = "verifying_company_names"
        pending = [
            lead
            for lead in task["leads"]
            if lead["city"] == city and not lead.get("company_name_verified_at")
        ]
        if max_candidates is not None:
            pending = pending[: max(1, int(max_candidates))]
        if not pending:
            return 0

        for lead in pending:
            self._control_checkpoint(task["id"])
            lead["company_name_verification_attempts"] = (
                int(lead.get("company_name_verification_attempts") or 0) + 1
            )
        self.store.save_leads(task["id"], pending)
        self._save_progress(task)

        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {
                executor.submit(
                    verify,
                    company_name=lead["company_name"],
                    city=city,
                    keywords=list(task.get("keywords") or []),
                    source_urls=list(
                        dict.fromkeys(
                            source_url
                            for source_url in [
                                *(lead.get("company_name_evidence_urls") or []),
                                str(lead.get("source_url") or "").strip(),
                            ]
                            if source_url
                        )
                    ),
                ): lead
                for lead in pending
            }
            for future in as_completed(futures):
                current = futures[future]
                try:
                    evidence = future.result()
                    status = str(evidence.status)
                    urls = list(evidence.evidence_urls)
                    reason = str(evidence.reason)
                except Exception as exc:  # noqa: BLE001 - isolate one candidate from the task
                    status = "verification_failed"
                    urls = []
                    reason = f"公司名称公开证据核验失败: {exc}"
                if status not in VERIFIED_COMPANY_NAME_STATUSES:
                    browser_evidence = self._verify_company_name_with_browser(
                        company_name=current["company_name"],
                        city=city,
                        keywords=list(task.get("keywords") or []),
                    )
                    if browser_evidence is not None:
                        status, urls, reason = browser_evidence
                current.update(
                    company_name_verification_status=status,
                    company_name_evidence_urls=urls,
                    company_name_verification_reason=reason,
                    company_name_verified_at=_now(),
                )
                if status not in VERIFIED_COMPANY_NAME_STATUSES:
                    current["enrichment_state"] = "blocked"
                    current["enrichment_error"] = reason
                self.store.save_leads(task["id"], [current])
                task["updated_at"] = _now()
                self._save_progress(task)
                self._control_checkpoint(task["id"])
        return len(pending)

    def _verify_company_name_with_browser(
        self,
        *,
        company_name: str,
        city: str,
        keywords: list[str],
    ) -> tuple[str, list[str], str] | None:
        gateway = self.doubao_web_gateway
        search = getattr(gateway, "search_public_web", None)
        if not callable(search):
            return None
        query = " ".join([company_name, city, *keywords[:3]])
        try:
            results = search(query=query, max_results=10)
        except Exception:  # noqa: BLE001 - browser search is a non-blocking fallback
            return None
        normalized_name = re.sub(r"\s+", "", company_name).casefold()
        city_short = city.removesuffix("市").casefold()
        keyword_terms = [
            re.sub(r"公司$", "", value.strip()).casefold()
            for value in keywords
            if len(re.sub(r"公司$", "", value.strip())) >= 2
        ]
        for result in results:
            text = f"{result.get('title', '')} {result.get('text', '')}"
            compact = re.sub(r"\s+", "", text).casefold()
            url = str(result.get("url") or "").strip()
            if normalized_name not in compact:
                continue
            city_matched = city_short in compact or city_short in normalized_name
            keyword_matched = any(
                term in compact or term in normalized_name for term in keyword_terms
            )
            if (
                city_matched
                and keyword_matched
                and url.startswith(("http://", "https://"))
            ):
                return (
                    "verified_public_web",
                    [url],
                    "浏览器公开搜索结果同时确认完整公司名称、城市和主营方向",
                )
        return None

    def _enrich_city_candidates(
        self,
        task: dict[str, Any],
        city: str,
        *,
        allow_unverified: bool = True,
    ) -> None:
        if task.get("use_doubao_web_chat"):
            self._enrich_city_candidates_web(
                task,
                city,
                allow_unverified=allow_unverified,
            )
            return
        task["collection_phase"] = "enriching"
        pending = [
            deepcopy
            for deepcopy in task["leads"]
            if deepcopy["city"] == city
            and deepcopy.get("enrichment_state") not in {"completed", "failed"}
            and (
                self.company_name_verifier is None
                or deepcopy.get("company_name_verification_status")
                in VERIFIED_COMPANY_NAME_STATUSES
            )
        ]
        if not pending:
            return
        pending_control: TaskPaused | TaskTerminated | None = None

        def remember_control(control: TaskPaused | TaskTerminated) -> None:
            nonlocal pending_control
            if pending_control is None or isinstance(control, TaskTerminated):
                pending_control = control

        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {
                executor.submit(self._enrich_with_doubao, task, dict(lead)): (
                    lead["city"],
                    lead["company_name"],
                )
                for lead in pending
            }
            for future in as_completed(futures):
                city_name, company_name = futures[future]
                try:
                    enriched, model_id, error, control = future.result()
                except (TaskPaused, TaskTerminated) as control:
                    remember_control(control)
                    continue
                index = next(
                    i
                    for i, lead in enumerate(task["leads"])
                    if lead["city"] == city_name and lead["company_name"] == company_name
                )
                current = task["leads"][index]
                if enriched is not None:
                    normalized = normalize_lead(
                        enriched,
                        city=city_name,
                        provider=model_id or current["discovery_channel"],
                        keyword=current.get("matched_keyword"),
                    )
                    verify_contact = getattr(
                        self.contact_verifier,
                        "verify_contact_evidence",
                        None,
                    )
                    if (
                        normalized.get("contact_verification_status")
                        not in VERIFIED_CONTACT_STATUSES
                        and callable(verify_contact)
                    ):
                        try:
                            evidence = verify_contact(
                                company_name=normalized["company_name"],
                                mobile=normalized.get("mobile"),
                                source_url=normalized.get("source_url"),
                                source_urls=list(normalized.get("contact_evidence_urls") or []),
                                contact_person_name=normalized.get("contact_person_name"),
                                contact_person_role=normalized.get("contact_person_role"),
                            )
                            contact_status = str(evidence.status)
                            contact_url = evidence.evidence_url
                            contact_urls = list(evidence.evidence_urls or [])
                            contact_quote = str(evidence.evidence_quote)
                            contact_reason = str(evidence.reason)
                            contact_name = evidence.contact_person_name
                            contact_role = evidence.contact_person_role
                        except Exception as exc:  # noqa: BLE001 - isolate one contact
                            contact_status = "verification_failed"
                            contact_url = normalized.get("source_url")
                            contact_urls = []
                            contact_quote = ""
                            contact_reason = f"联系方式公开证据核验失败: {exc}"
                            contact_name = None
                            contact_role = None
                        normalized.update(
                            contact_verification_status=contact_status,
                            contact_evidence_url=contact_url,
                            contact_evidence_urls=contact_urls,
                            contact_evidence_quote=contact_quote,
                            contact_verified_at=_now(),
                            contact_verification_reason=contact_reason,
                        )
                        if contact_status in VERIFIED_CONTACT_STATUSES:
                            normalized["contact_person_name"] = contact_name
                            normalized["contact_person_role"] = contact_role
                        else:
                            normalized["contact_person_name"] = None
                            normalized["contact_person_role"] = None
                    for field in (
                        "mobile",
                        "contact_person_name",
                        "contact_person_role",
                        "contact_verification_status",
                        "contact_evidence_url",
                        "contact_evidence_urls",
                        "contact_evidence_quote",
                        "contact_evidence_quotes",
                        "contact_evidence_grade",
                        "contact_evidence_score",
                        "contact_verified_at",
                        "contact_verification_reason",
                        "email",
                        "wechat",
                        "address",
                        "service_intro",
                        "source_url",
                        "source_confidence",
                        "confidence_score",
                        "saved_at",
                    ):
                        current[field] = normalized[field]
                    current["enrichment_state"] = "completed"
                    current["enrichment_model"] = model_id
                else:
                    current["enrichment_state"] = "failed"
                    current["enrichment_error"] = error
                    task["unit_errors"] += 1
                task["enrichment_completed"] = sum(
                    lead.get("enrichment_state") in TERMINAL_ENRICHMENT_STATES
                    for lead in task["leads"]
                )
                self.store.save_leads(task["id"], [current])
                task["updated_at"] = _now()
                self._save_progress(task)
                if control is not None:
                    remember_control(control)
                try:
                    self._deliver_lead(task, current, check_after=False)
                except (TaskPaused, TaskTerminated) as control:
                    remember_control(control)
                else:
                    try:
                        self._control_checkpoint(task["id"])
                    except (TaskPaused, TaskTerminated) as control:
                        remember_control(control)
        try:
            self._control_checkpoint(task["id"])
        except (TaskPaused, TaskTerminated) as control:
            remember_control(control)
        if pending_control is not None:
            raise pending_control
        task["leads"] = deduplicate_leads(task["leads"])
        city_leads = [lead for lead in task["leads"] if lead["city"] == city]
        self.store.reconcile_leads(task["id"], city, city_leads)
        task["candidate_count"] = len(task["leads"])
        task["enrichment_total"] = len(task["leads"])
        task["enrichment_completed"] = sum(
            lead.get("enrichment_state") in TERMINAL_ENRICHMENT_STATES
            for lead in task["leads"]
        )
        task["lead_count"] = len(task["leads"])
        self._save_progress(task)

    def _enrich_city_candidates_web(
        self,
        task: dict[str, Any],
        city: str,
        *,
        allow_unverified: bool = True,
    ) -> None:
        gateway = self.doubao_web_gateway
        if gateway is None:
            raise InvalidTaskStateError("豆包网页采集未启用")
        task["collection_phase"] = "enriching"
        task["web_chat_status"] = "working"
        # A discovery timeout is a historical fallback notice, not a current
        # blocker for the enrichment pass. Clear it before waiting on the next
        # company so the UI cannot present stale timeout text as an interruption.
        task["web_chat_error"] = None
        if self._hydrate_candidate_contacts_in_place(task):
            task["updated_at"] = _now()
            self._save_progress(task)
        checkpoint = task.setdefault("web_chat_checkpoint", {})
        processed = list(checkpoint.get("processed_normalized_names") or [])
        # Older task snapshots predate retry windows. Defer their known timeout
        # failures once so a restart can move on to discovery instead of replaying
        # the same poisoned browser command immediately.
        migrated_retry_windows = False
        for lead in task["leads"]:
            if (
                lead.get("city") == city
                and lead.get("enrichment_state") == "failed"
                and not lead.get("enrichment_retry_after")
                and "豆包网页操作超时" in str(lead.get("enrichment_error") or "")
            ):
                lead["enrichment_retry_after"] = (
                    self._now() + timedelta(seconds=_DOUBAO_FAILED_RETRY_COOLDOWN_SECONDS)
                ).isoformat()
                migrated_retry_windows = True
        if migrated_retry_windows:
            task["updated_at"] = _now()
            self._save_progress(task)
        retryable_keys = {
            (normalize_company_name(str(lead.get("company_name") or "")) or "").casefold()
            for lead in task["leads"]
            if lead.get("city") == city
            and lead.get("enrichment_state") not in {"completed", "blocked"}
            and self._enrichment_retry_is_due(lead)
        }
        # A crash can happen after the checkpoint is written but before the
        # candidate reaches a terminal state. Remove all such stale markers so
        # pending and failed candidates resume instead of being skipped forever.
        processed = [
            name
            for name in processed
            if (normalize_company_name(str(name)) or "").casefold() not in retryable_keys
        ]
        processed_keys = {
            (normalize_company_name(str(name)) or "").casefold() for name in processed
        }
        # Let a fresh candidate run before retrying an older failure. A failed
        # web lookup remains eligible on the next pass, but must not starve the
        # pending candidates discovered later in the same round.
        city_leads = sorted(
            (lead for lead in task["leads"] if lead["city"] == city),
            key=lambda lead: lead.get("enrichment_state") == "failed",
        )
        for company_index, current in enumerate(city_leads):
            company_name = str(current.get("company_name") or "").strip()
            normalized_name = normalize_company_name(company_name) or company_name
            company_key = normalized_name.casefold()
            if (
                current.get("enrichment_state") == "failed"
                and not self._enrichment_retry_is_due(current)
            ):
                continue
            if (
                company_key in processed_keys
                or current.get("enrichment_state") == "completed"
                or (
                    self.company_name_verifier is not None
                    and current.get("company_name_verification_status") == "rejected"
                )
                or (
                    not allow_unverified
                    and self.company_name_verifier is not None
                    and current.get("company_name_verification_status")
                    not in VERIFIED_COMPANY_NAME_STATUSES
                )
            ):
                continue
            if self._verify_discovered_contact(current):
                current["enrichment_state"] = "completed"
                current["enrichment_model"] = "doubao_web"
                if normalized_name not in processed:
                    processed.append(normalized_name)
                    processed_keys.add(company_key)
                checkpoint.update(
                    city=city,
                    company_index=company_index + 1,
                    processed_normalized_names=processed,
                )
                task["enrichment_completed"] = sum(
                    lead.get("enrichment_state") in TERMINAL_ENRICHMENT_STATES
                    for lead in task["leads"]
                )
                self.store.save_leads(task["id"], [current])
                task["updated_at"] = _now()
                self._save_progress(task)
                self._deliver_lead(task, current, check_after=False)
                self._control_checkpoint(task["id"])
                continue
            checkpoint.update(
                city=city,
                company_index=company_index,
                processed_normalized_names=processed,
            )
            task["web_chat_current_company"] = company_name
            self._save_progress(task)
            self._control_checkpoint(task["id"])
            contact = None
            enrichment_error: Exception | None = None
            # Parser errors get one immediate retry. A timeout or a transient
            # browser disconnect gets one extra attempt after reconnecting the
            # same persistent browser session, so one slow company cannot poison
            # the rest of the batch.
            # A timeout already consumes the full browser wait budget. Retry it
            # once after reconnecting, then mark this company failed so the
            # collection loop can continue with the next candidate.
            for attempt in range(2):
                try:
                    contact = gateway.enrich_company(
                        company_name=company_name,
                        city=city,
                        requested_contacts=list(task.get("required_contacts") or ["mobile"]),
                    )
                    break
                except (DoubaoLoginRequired, DoubaoChallengeRequired) as exc:
                    self._wait_for_doubao_login(task, exc, company_name=company_name)
                except (DoubaoWebResponseError, DoubaoWebUnavailable, DoubaoWebTimeout) as exc:
                    enrichment_error = exc
                    if isinstance(exc, DoubaoWebResponseError) and attempt >= 1:
                        break
                    if attempt >= 1:
                        break
                    if isinstance(exc, (DoubaoWebUnavailable, DoubaoWebTimeout)):
                        reconnect = getattr(gateway, "reconnect", None)
                        if callable(reconnect):
                            try:
                                reconnect()
                                task.setdefault("activity", []).append(
                                    self._activity(
                                        "doubao_web_reconnect",
                                        city=city,
                                        keyword=company_name,
                                        outcome="recovered",
                                        detail="单家公司查询异常后复用同一豆包会话重连",
                                    )
                                )
                            except Exception as reconnect_error:  # noqa: BLE001 - retry the candidate
                                enrichment_error = reconnect_error
                                task.setdefault("activity", []).append(
                                    self._activity(
                                        "doubao_web_reconnect",
                                        city=city,
                                        keyword=company_name,
                                        outcome="error",
                                        detail=str(reconnect_error),
                                    )
                                )
                        self._sleep(min(2.0, 0.5 * (attempt + 1)))
                    self._control_checkpoint(task["id"])
                    continue
                except Exception as exc:  # noqa: BLE001 - isolate one web-chat candidate
                    enrichment_error = exc
                    break

            if contact is None:
                current["enrichment_state"] = "failed"
                current["enrichment_error"] = str(enrichment_error or "豆包联系方式查询失败")
                task["unit_errors"] += 1
                if isinstance(enrichment_error, (DoubaoWebTimeout, DoubaoWebUnavailable)):
                    current["enrichment_retry_after"] = (
                        self._now()
                        + timedelta(seconds=_DOUBAO_FAILED_RETRY_COOLDOWN_SECONDS)
                    ).isoformat()
            else:
                raw_contact = {
                    "company_name": contact.company_name,
                    "mobile": contact.mobile,
                    "contact_person_name": contact.contact_person_name,
                    "contact_person_role": contact.contact_person_role,
                    "email": contact.email,
                    "wechat": contact.wechat,
                    "address": contact.address,
                    "service_intro": contact.service_intro,
                    "source_url": contact.source_urls[0] if contact.source_urls else None,
                    "contact_evidence_urls": list(contact.source_urls),
                    "contact_evidence_quotes": list(contact.evidence_quotes),
                    "matched_keyword": current.get("matched_keyword"),
                    "source_confidence": current.get("source_confidence") or 70,
                }
                normalized = normalize_lead(
                    raw_contact,
                    city=city,
                    provider="doubao_web",
                    keyword=current.get("matched_keyword"),
                )
                self._merge_enriched_contact_with_candidate(current, normalized)
                # Persist the raw Doubao contact immediately. Evidence checks
                # can involve another network lookup; they must not hide a
                # number that has already been returned to the user.
                for field in (
                    "mobile",
                    "contact_person_name",
                    "contact_person_role",
                    "contact_verification_status",
                    "contact_evidence_url",
                    "contact_evidence_quote",
                    "contact_evidence_urls",
                    "contact_evidence_quotes",
                    "contact_evidence_grade",
                    "contact_evidence_score",
                    "contact_verified_at",
                    "contact_verification_reason",
                    "email",
                    "wechat",
                    "address",
                    "service_intro",
                    "source_url",
                    "source_confidence",
                    "confidence_score",
                    "saved_at",
                ):
                    current[field] = normalized.get(field)
                current["enrichment_model"] = "doubao_web"
                self.store.save_leads(task["id"], [current])
                task["updated_at"] = _now()
                self._save_progress(task)
                proposed_mobile = normalized.get("mobile")
                self._verify_web_contact(normalized)
                if (
                    normalized.get("contact_verification_status")
                    not in VERIFIED_CONTACT_STATUSES
                    and proposed_mobile
                ):
                    self._verify_discovered_contact_with_browser(
                        normalized,
                        str(proposed_mobile),
                    )
                self._promote_contact_backed_company(current, normalized)
                for field in (
                    "mobile",
                    "contact_person_name",
                    "contact_person_role",
                    "contact_verification_status",
                    "contact_evidence_url",
                    "contact_evidence_quote",
                    "contact_evidence_urls",
                    "contact_evidence_quotes",
                    "contact_evidence_grade",
                    "contact_evidence_score",
                    "contact_verified_at",
                    "contact_verification_reason",
                    "email",
                    "wechat",
                    "address",
                    "service_intro",
                    "source_url",
                    "source_confidence",
                    "confidence_score",
                    "saved_at",
                ):
                    current[field] = normalized.get(field)
                current["enrichment_state"] = "completed"
                current["enrichment_model"] = "doubao_web"
                current.pop("enrichment_retry_after", None)

            if (
                current.get("enrichment_state") != "failed"
                and normalized_name not in processed
            ):
                processed.append(normalized_name)
                processed_keys.add(company_key)
            checkpoint.update(
                company_index=company_index + 1,
                processed_normalized_names=processed,
            )
            task["web_chat_current_company"] = None
            task["enrichment_completed"] = sum(
                lead.get("enrichment_state") in TERMINAL_ENRICHMENT_STATES
                for lead in task["leads"]
            )
            self.store.save_leads(task["id"], [current])
            task["updated_at"] = _now()
            self._save_progress(task)
            self._deliver_lead(task, current, check_after=False)
            self._control_checkpoint(task["id"])

        task["web_chat_status"] = "ready"
        task["web_chat_error"] = None
        task["web_chat_current_company"] = None
        task["leads"] = deduplicate_leads(task["leads"])
        city_leads = [lead for lead in task["leads"] if lead["city"] == city]
        self.store.reconcile_leads(task["id"], city, city_leads)
        task["candidate_count"] = len(task["leads"])
        task["enrichment_total"] = len(task["leads"])
        task["enrichment_completed"] = sum(
            lead.get("enrichment_state") in TERMINAL_ENRICHMENT_STATES
            for lead in task["leads"]
        )
        task["lead_count"] = len(task["leads"])
        self._save_progress(task)

    def _enrichment_retry_is_due(self, lead: dict[str, Any]) -> bool:
        retry_after = str(lead.get("enrichment_retry_after") or "").strip()
        if not retry_after:
            return True
        try:
            return datetime.fromisoformat(retry_after) <= self._now()
        except ValueError:
            return True

    @staticmethod
    def _merge_enriched_contact_with_candidate(
        current: dict[str, Any],
        normalized: dict[str, Any],
    ) -> None:
        """Keep candidate fields when a later web lookup has no replacement."""
        preserve_if_empty = (
            "email",
            "wechat",
            "address",
            "service_intro",
            "source_url",
            "contact_evidence_url",
            "contact_evidence_quote",
            "contact_evidence_urls",
            "contact_evidence_quotes",
            "contact_verification_reason",
        )
        for field in preserve_if_empty:
            if not normalized.get(field) and current.get(field):
                normalized[field] = current[field]

        # A candidate phone may already come from a public discovery result.
        # A sparse Doubao answer must not erase it or its stronger evidence.
        if not normalized.get("mobile") and current.get("mobile"):
            for field in (
                "mobile",
                "contact_person_name",
                "contact_person_role",
                "contact_verification_status",
                "contact_evidence_url",
                "contact_evidence_quote",
                "contact_evidence_urls",
                "contact_evidence_quotes",
                "contact_evidence_grade",
                "contact_evidence_score",
                "contact_verified_at",
                "contact_verification_reason",
            ):
                if current.get(field) is not None:
                    normalized[field] = current[field]

    def _verify_discovered_contact(self, current: dict[str, Any]) -> bool:
        candidate_text = str(current.get("candidate_reason") or "")
        structured_mobile = re.sub(r"\D", "", str(current.get("mobile") or ""))
        raw_mobiles = [
            structured_mobile if re.fullmatch(r"1[3-9]\d{9}", structured_mobile) else "",
            *re.findall(r"(?<!\d)1[3-9]\d{9}(?!\d)", candidate_text),
        ]
        candidate_mobiles = list(dict.fromkeys(value for value in raw_mobiles if value))
        candidate_contacts: list[tuple[str, str]] = [
            ("mobile", value) for value in candidate_mobiles
        ]
        candidate_emails = []
        candidate_emails.extend(_candidate_emails(current))
        candidate_emails.extend(_EMAIL_PATTERN.findall(candidate_text))
        candidate_contacts.extend(
            ("email", value.lower())
            for value in dict.fromkeys(candidate_emails)
            if value
        )
        source_urls = list(
            dict.fromkeys(
                value
                for value in [
                    str(current.get("source_url") or "").strip(),
                    *(current.get("company_name_evidence_urls") or []),
                ]
                if value
            )
        )
        if not candidate_contacts or not source_urls:
            return False

        for contact_kind, contact_value in candidate_contacts:
            candidate = dict(current)
            candidate.update(
                mobile=contact_value if contact_kind == "mobile" else current.get("mobile"),
                email=contact_value if contact_kind == "email" else current.get("email"),
                contact_person_name=None,
                contact_person_role=None,
                source_url=source_urls[0],
                contact_evidence_urls=source_urls,
            )
            self._verify_web_contact(candidate)
            if (
                candidate.get("contact_verification_status")
                not in VERIFIED_CONTACT_STATUSES
                and not (
                    contact_kind == "mobile"
                    and self._verify_discovered_contact_with_browser(candidate, contact_value)
                )
            ):
                continue
            for field in (
                "mobile",
                "email",
                "contact_person_name",
                "contact_person_role",
                "contact_verification_status",
                "contact_evidence_url",
                "contact_evidence_quote",
                "contact_evidence_urls",
                "contact_evidence_quotes",
                "contact_evidence_grade",
                "contact_evidence_score",
                "contact_verified_at",
                "contact_verification_reason",
                "source_url",
            ):
                current[field] = candidate.get(field)
            self._promote_contact_backed_company(current, candidate)
            return True
        return False

    def _promote_contact_backed_company(
        self,
        current: dict[str, Any],
        contact: dict[str, Any],
    ) -> None:
        """Use an exact public company/contact match to finish a pending name check.

        A source-backed candidate may fail the separate company-name search because
        an index page is incomplete or temporarily unavailable.  We still require
        the candidate source and the contact evidence to mention the target city
        before promoting it; a contact match alone is never enough.
        """
        if (
            current.get("company_name_verification_status")
            in VERIFIED_COMPANY_NAME_STATUSES
            or contact.get("contact_verification_status")
            not in VERIFIED_CONTACT_STATUSES
        ):
            return
        city = str(current.get("city") or "").strip()
        city_short = city.removesuffix("市")
        evidence_text = " ".join(
            str(value or "")
            for value in (
                current.get("company_name"),
                current.get("candidate_reason"),
                current.get("company_name_verification_reason"),
                contact.get("contact_evidence_quote"),
                *(contact.get("contact_evidence_quotes") or []),
            )
        )
        compact = re.sub(r"\s+", "", evidence_text)
        if city_short and city_short not in compact:
            return
        contact_status = str(contact.get("contact_verification_status"))
        company_status = (
            "verified_official_page"
            if contact_status == "verified_official_page"
            else "verified_public_web"
        )
        evidence_urls = list(
            dict.fromkeys(
                str(value).strip()
                for value in (
                    *(current.get("company_name_evidence_urls") or []),
                    *(contact.get("contact_evidence_urls") or []),
                    contact.get("contact_evidence_url"),
                )
                if str(value or "").strip()
            )
        )
        current.update(
            company_name_verification_status=company_status,
            company_name_evidence_urls=evidence_urls,
            company_name_verification_reason=(
                "联系方式公开证据确认完整公司名称和手机号，候选来源同时指向目标城市"
            ),
            company_name_verified_at=_now(),
        )

    def _verify_discovered_contact_with_browser(
        self,
        candidate: dict[str, Any],
        mobile: str,
    ) -> bool:
        gateway = self.doubao_web_gateway
        search = getattr(gateway, "search_public_web", None)
        if not callable(search):
            return False
        company_name = str(candidate.get("company_name") or "").strip()
        try:
            results = search(query=f"{company_name} {mobile}", max_results=10)
        except Exception:  # noqa: BLE001 - browser search is a non-blocking fallback
            return False
        normalized_name = re.sub(r"\s+", "", company_name).casefold()
        for result in results:
            text = f"{result.get('title', '')} {result.get('text', '')}"
            compact = re.sub(r"\s+", "", text).casefold()
            url = str(result.get("url") or "").strip()
            if (
                normalized_name not in compact
                or mobile not in re.sub(r"\D", "", text)
                or not url.startswith(("http://", "https://"))
            ):
                continue
            candidate.update(
                mobile=mobile,
                contact_person_name=None,
                contact_person_role=None,
                contact_verification_status="verified_public_web",
                contact_evidence_url=url,
                contact_evidence_urls=[url],
                contact_evidence_quote=text.strip()[:1000],
                contact_verified_at=_now(),
                contact_verification_reason=(
                    "浏览器公开搜索结果同时确认完整公司名称和手机号"
                ),
            )
            return True
        return False

    def _verify_web_contact(self, normalized: dict[str, Any]) -> None:
        proposed_mobile = normalized.get("mobile")
        proposed_email = normalized.get("email")
        proposed_person_name = normalized.get("contact_person_name")
        proposed_person_role = normalized.get("contact_person_role")
        verify_contact = getattr(self.contact_verifier, "verify_contact_evidence", None)
        if not callable(verify_contact):
            return
        try:
            evidence = verify_contact(
                company_name=normalized["company_name"],
                mobile=normalized.get("mobile"),
                email=normalized.get("email"),
                source_url=normalized.get("source_url"),
                source_urls=list(normalized.get("contact_evidence_urls") or []),
                contact_person_name=normalized.get("contact_person_name"),
                contact_person_role=normalized.get("contact_person_role"),
            )
            status = str(evidence.status)
            normalized.update(
                contact_verification_status=status,
                contact_evidence_url=evidence.evidence_url,
                contact_evidence_urls=list(evidence.evidence_urls or []),
                contact_evidence_quote=str(evidence.evidence_quote),
                contact_verified_at=_now(),
                contact_verification_reason=str(evidence.reason),
            )
        except Exception as exc:  # noqa: BLE001 - isolate one evidence source
            status = "verification_failed"
            normalized.update(
                contact_verification_status=status,
                contact_verified_at=_now(),
                contact_verification_reason=f"联系方式公开证据核验失败: {exc}",
            )
        if status in VERIFIED_CONTACT_STATUSES:
            normalized["contact_person_name"] = evidence.contact_person_name
            normalized["contact_person_role"] = evidence.contact_person_role
        else:
            # Keep model-supplied fields visible in the collection table. The
            # ERP gate still requires verified evidence, so preserving raw
            # collection output does not make it eligible for delivery.
            normalized["mobile"] = proposed_mobile
            normalized["email"] = proposed_email
            normalized["contact_person_name"] = proposed_person_name
            normalized["contact_person_role"] = proposed_person_role

    def _enrich_with_doubao(
        self,
        task: dict[str, Any],
        company: dict[str, Any],
    ) -> tuple[
        dict[str, Any] | None,
        str | None,
        str | None,
        TaskPaused | TaskTerminated | None,
    ]:
        model_id = "doubao"
        verifier = self.verifiers.get(model_id)
        enrich = getattr(verifier, "enrich_company", None)
        if not callable(enrich):
            return None, None, "doubao: enrichment model unavailable", None
        errors: list[str] = []
        for _ in range(2):
            try:
                self._control_checkpoint(task["id"])
                enriched = enrich(
                    request_text=task["request_text"],
                    city=company["city"],
                    company=company,
                    required_contacts=task.get("required_contacts") or ["mobile"],
                )
            except (ModelVerificationError, TypeError, ValueError) as exc:
                errors.append(f"doubao: {exc}")
                self._control_checkpoint(task["id"])
                continue
            try:
                self._control_checkpoint(task["id"])
            except (TaskPaused, TaskTerminated) as control:
                return enriched, model_id, None, control
            return enriched, model_id, None, None
        return None, None, "; ".join(errors), None

    def _deliver_lead(
        self,
        task: dict[str, Any],
        lead: dict[str, Any],
        *,
        check_after: bool = True,
    ) -> None:
        has_contact = bool(lead.get("mobile") or lead.get("email") or lead.get("wechat"))
        if not task.get("auto_deliver") or not lead.get("company_name") or not has_contact:
            lead.update(
                delivery_status="display_only",
                delivery_error=None,
                delivered_at=None,
                delivery_fingerprint=None,
            )
            self.store.save_leads(task["id"], [lead])
            return
        self._send_lead(task, lead, check_after=check_after)

    def _deliver_verified_lead(
        self,
        task: dict[str, Any],
        lead: dict[str, Any],
        *,
        check_after: bool = True,
    ) -> None:
        reason = verified_delivery_ineligibility(lead)
        if not task.get("verified_auto_deliver") or reason:
            if lead.get("delivery_status") != "succeeded":
                lead.update(
                    delivery_status="display_only",
                    delivery_error=reason,
                    delivered_at=None,
                    delivery_fingerprint=None,
                )
                self.store.save_leads(task["id"], [lead])
            return
        self._send_lead(task, lead, check_after=check_after)

    def _send_lead(
        self,
        task: dict[str, Any],
        lead: dict[str, Any],
        *,
        check_after: bool = True,
    ) -> None:
        if lead.get("delivery_status") == "sending":
            return
        # A manual push can claim the same row while the collector is holding
        # the task operation lock. Refresh just the delivery state before an
        # automatic send so the collector never starts a duplicate request.
        current_task = self.store.load(task["id"])
        current_lead = next(
            (
                item
                for item in current_task.get("leads", [])
                if _delivery_identity(item) == _delivery_identity(lead)
            ),
            None,
        )
        if current_lead is not None and current_lead.get("delivery_status") == "sending":
            _copy_delivery_state(current_lead, lead)
            return
        fingerprint_method = getattr(self.lead_delivery, "fingerprint", None)
        current_fingerprint = (
            fingerprint_method(task, lead) if callable(fingerprint_method) else None
        )
        if lead.get("delivery_status") in {"succeeded", "failed"} and (
            current_fingerprint is None
            or lead.get("delivery_fingerprint") == current_fingerprint
        ):
            return
        if lead.get("delivery_status") != "pending":
            _advance_delivery_state(lead, "pending")
            lead.update(delivery_error=None, delivered_at=None)
            self.store.save_leads(task["id"], [lead])
        self._control_checkpoint(task["id"])
        operation_id = str(uuid4())
        _advance_delivery_state(lead, "sending", operation_id=operation_id)
        lead.update(delivery_error=None, delivered_at=None)
        self.store.save_leads(task["id"], [lead])
        result_fingerprint = current_fingerprint
        try:
            if self.lead_delivery is None:
                raise RuntimeError("ERP automatic delivery is not configured")
            result = self.lead_delivery.deliver(task, lead)
            status = str(getattr(result, "status", "failed"))
            error = getattr(result, "error", None)
            result_fingerprint = getattr(result, "fingerprint", None)
        except Exception as exc:  # noqa: BLE001 - ERP failures must not stop collection
            status = "failed"
            error = str(exc)
        succeeded = status in {"completed", "skipped"}
        _advance_delivery_state(
            lead,
            "succeeded" if succeeded else "failed",
            operation_id=operation_id,
        )
        lead.update(
            delivery_error=None if succeeded else (str(error) if error else "ERP delivery failed"),
            delivered_at=_now() if succeeded else None,
            delivery_fingerprint=result_fingerprint,
        )
        self.store.save_leads(task["id"], [lead])
        outcome = "completed" if succeeded else "failed"
        task.setdefault("activity", []).append(
            self._activity(
                "erp_delivery",
                city=str(lead.get("city") or ""),
                provider="erp",
                outcome=outcome,
                detail=str(error)[:500] if error else None,
            )
        )
        self._save_progress(task)
        if check_after:
            self._control_checkpoint(task["id"])

    def _deliver_pending_leads(self, task: dict[str, Any]) -> None:
        for lead in task.get("leads", []):
            if lead.get("delivery_status") != "pending":
                continue
            self._deliver_lead(task, lead)

    def _deliver_verified_pending_leads(self, task: dict[str, Any]) -> None:
        for lead in task.get("leads", []):
            if lead.get("delivery_status") == "succeeded":
                continue
            self._deliver_verified_lead(task, lead)

    @staticmethod
    def _activity(
        action: str,
        *,
        outcome: str,
        city: str | None = None,
        keyword: str | None = None,
        provider: str | None = None,
        detail: str | None = None,
    ) -> dict[str, Any]:
        return {
            "timestamp": _now(),
            "action": action,
            "city": city,
            "keyword": keyword,
            "provider": provider,
            "outcome": outcome,
            **({"detail": detail} if detail else {}),
        }

    @staticmethod
    def _update_city_progress(task: dict[str, Any], city: str) -> None:
        prefix = f'["{city}",'
        completed = sum(key.startswith(prefix) for key in task["completed_unit_keys"])
        progress = task["city_progress"][city]
        progress["completed_units"] = completed
        progress["lead_count"] = sum(lead["city"] == city for lead in task["leads"])
        progress["status"] = "completed" if completed == progress["total_units"] else "running"
