from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic, sleep
from types import SimpleNamespace

from app.collection.continuous import (
    DEFAULT_VALID_LEAD_TARGET,
    ROUND_CANDIDATE_LIMIT,
    ROUND_COOLDOWN_SECONDS,
    TARGET_ROUND_COOLDOWN_SECONDS,
    candidate_pool_is_sufficient,
    discovery_query_groups,
    evidence_backed_contact_count,
    new_round_metrics,
    rotate_keywords,
    strict_verified_lead_count,
    target_contact_count,
    target_contact_mode,
    verified_delivery_ineligibility,
    verified_erp_ineligibility,
)
from app.collection.models import ModelAccountError
from app.collection.service import CollectionService, TaskStore


def test_discovery_query_groups_expand_business_synonyms_without_duplicates() -> None:
    groups = discovery_query_groups(
        ["别墅软装公司", "全案设计公司"],
        round_number=1,
    )

    assert groups[0] == ["别墅软装公司", "全案设计公司"]
    flattened = [keyword for group in groups for keyword in group]
    assert "私宅设计公司" in flattened
    assert "大宅设计公司" in flattened
    assert "软装陈设公司" in flattened
    assert "全案落地公司" in flattened
    assert "高端住宅设计公司" in flattened
    assert "室内设计公司" in flattened
    assert "整装落地公司" in flattened
    assert len(flattened) == len(set(flattened))


class RoundVerifier:
    configured = True

    def __init__(self, model_id: str) -> None:
        self.id = model_id
        self.label = "豆包" if model_id == "doubao" else "DeepSeek"
        self.discovery_calls = 0

    def discover_companies(
        self,
        *,
        request_text: str,
        city: str,
        keywords: list[str],
        limit: int = 100,
    ) -> list[dict]:
        self.discovery_calls += 1
        return [
            {
                "company_name": "深圳持续软装有限公司",
                "matched_keyword": keywords[0],
                "source_confidence": 85,
            }
        ]

    def enrich_company(
        self,
        *,
        request_text: str,
        city: str,
        company: dict,
        required_contacts: list[str],
    ) -> dict:
        return {
            **company,
            "mobile": "13800138000",
            "address": "深圳市公开地址",
            "service_intro": request_text,
            "source_url": "https://example.test/shenzhen-company",
            "source_confidence": 85,
        }

    def verify(
        self,
        leads: list[dict],
        *,
        request_text: str,
        city: str,
        keyword: str,
    ) -> list[dict]:
        return [
            {"relevant": True, "confidence": 90, "reason": "符合任务"}
            for _ in leads
        ]


class AccountBlockedVerifier:
    id = "deepseek"
    label = "DeepSeek"
    configured = True

    def discover_companies(self, **kwargs) -> list[dict]:
        raise ModelAccountError("AccountOverdueError: account has an overdue balance")


class FakeClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 28, tzinfo=UTC)

    def now(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


class StrictCompanyVerifier:
    def verify_company_name(self, **kwargs) -> SimpleNamespace:
        return SimpleNamespace(
            status="verified_official_page",
            evidence_urls=["https://example.cn/company"],
            reason="官网确认",
        )


class StrictContactVerifier:
    def verify_contact_evidence(self, **kwargs) -> SimpleNamespace:
        return SimpleNamespace(
            status="verified_official_page",
            evidence_url="https://example.cn/company/contact",
            evidence_urls=["https://example.cn/company/contact"],
            evidence_quote="深圳持续软装有限公司 13800138000",
            reason="同页确认",
            contact_person_name=None,
            contact_person_role=None,
        )


def _payload() -> dict:
    return {
        "name": "深圳持续采集",
        "request_text": "查深圳专做别墅项目的软装公司和全案设计公司，需要手机号",
        "cities": ["深圳市"],
        "all_cities": False,
        "keywords": ["别墅软装公司", "别墅全案设计公司"],
        "providers": ["web_search"],
        "models": ["deepseek"],
        "required_contacts": ["mobile"],
        "contact_match": "any",
        "limit_per_city": None,
        "continuous_mode": True,
    }


def _wait_for_status(service: CollectionService, task_id: str, status: str) -> dict:
    deadline = monotonic() + 3
    while monotonic() < deadline:
        task = service.get_task(task_id)
        if task["status"] == status:
            return task
        sleep(0.01)
    raise AssertionError(f"task did not reach {status}")


def test_continuous_task_pauses_on_non_retryable_model_account_error(tmp_path) -> None:
    service = CollectionService(
        TaskStore(tmp_path),
        {},
        {"deepseek": AccountBlockedVerifier()},
        max_concurrent_tasks=1,
    )
    task_id = service.create_task({**_payload(), "providers": []})["id"]
    try:
        service.start_task(task_id)
        paused = _wait_for_status(service, task_id, "paused")

        assert paused["pause_reason"] == "model_account_blocked"
        assert "AccountOverdueError" in paused["last_blocking_error"]
    finally:
        service.shutdown()


def _eligible_lead() -> dict:
    return {
        "company_name": "测试软装公司",
        "mobile": "13800138000",
        "source_url": "https://example.cn/company",
        "contact_verification_status": "verified_official_page",
        "contact_evidence_url": "https://example.cn/company/contact",
        "verification_status": "single_model_match",
        "company_name_verification_status": "verified_official_page",
    }


def test_continuous_round_policy_uses_fixed_limits() -> None:
    assert DEFAULT_VALID_LEAD_TARGET == 100
    assert ROUND_CANDIDATE_LIMIT == 100
    assert ROUND_COOLDOWN_SECONDS == 600
    assert TARGET_ROUND_COOLDOWN_SECONDS == 30
    assert new_round_metrics() == {
        "candidates": 0,
        "new_companies": 0,
        "verification_matches": 0,
        "verification_rejects": 0,
        "erp_succeeded": 0,
        "erp_failed": 0,
    }


def test_continuous_task_switches_to_enrichment_when_candidate_pool_is_sufficient() -> None:
    assert candidate_pool_is_sufficient(462, 100) is True
    assert candidate_pool_is_sufficient(99, 100) is False
    assert candidate_pool_is_sufficient(0, 0) is False


def test_continuous_task_defaults_to_one_hundred_strict_valid_leads(tmp_path: Path) -> None:
    service = CollectionService(TaskStore(tmp_path), {}, {})
    try:
        task = service.create_task(_payload())

        assert task["valid_lead_target"] == 100
        assert CollectionService.task_summary(task)["valid_lead_target"] == 100
    finally:
        service.shutdown()


def test_continuous_task_uses_explicit_quantity_as_strict_valid_target(tmp_path: Path) -> None:
    service = CollectionService(TaskStore(tmp_path), {}, {})
    try:
        task = service.create_task({**_payload(), "limit_per_city": 30})

        assert task["valid_lead_target"] == 30
    finally:
        service.shutdown()


def test_non_continuous_task_has_no_strict_valid_target(tmp_path: Path) -> None:
    service = CollectionService(TaskStore(tmp_path), {}, {})
    try:
        task = service.create_task({**_payload(), "continuous_mode": False})

        assert task["valid_lead_target"] is None
    finally:
        service.shutdown()


def test_legacy_continuous_task_gets_default_strict_valid_target(tmp_path: Path) -> None:
    store = TaskStore(tmp_path)
    service = CollectionService(store, {}, {})
    try:
        task = service.create_task(_payload())
        task.pop("valid_lead_target")
        store.save(task)

        assert service.get_task(task["id"])["valid_lead_target"] == 100
    finally:
        service.shutdown()


def test_strict_verified_lead_count_ignores_model_only_contacts() -> None:
    verified = _eligible_lead()
    unverified = {**verified, "contact_verification_status": "unverified_model_only"}

    assert strict_verified_lead_count([verified, unverified]) == 1


def test_strict_verified_lead_count_accepts_verified_email_without_mobile() -> None:
    lead = {
        **_eligible_lead(),
        "mobile": None,
        "email": "contact@example.cn",
    }

    assert strict_verified_lead_count([lead]) == 1


def test_evidence_backed_target_counts_phone_or_email_without_strict_verdict() -> None:
    leads = [
        {
            "mobile": "13800138000",
            "source_url": "https://example.cn/company-phone",
            "contact_verification_status": "rejected_mismatch",
        },
        {
            "email": "info@example.cn",
            "contact_evidence_url": "https://example.cn/company-email",
            "contact_verification_status": "unverified",
        },
        {
            "mobile": "020-12345678",
            "source_url": "https://example.cn/invalid-phone",
        },
        {
            "email": "not-an-email",
            "source_url": "https://example.cn/invalid-email",
        },
        {
            "mobile": "13900139000",
        },
    ]

    assert evidence_backed_contact_count(leads) == 2
    assert target_contact_mode({"request_text": "手机号或邮箱任一即可"}) == "evidence_backed"
    assert (
        target_contact_count(
            {
                "request_text": "手机号或邮箱任一即可，不能编造",
                "leads": leads,
            }
        )
        == 2
    )


def test_target_contact_count_keeps_strict_default_for_legacy_tasks() -> None:
    lead = _eligible_lead()

    assert target_contact_mode({"request_text": "需要手机号"}) == "strict"
    assert target_contact_count({"request_text": "需要手机号", "leads": [lead]}) == 1
    assert target_contact_count({"request_text": "需要手机号", "leads": [{**lead, "contact_verification_status": "unverified"}]}) == 0


def test_new_round_metrics_returns_independent_dicts() -> None:
    first = new_round_metrics()
    second = new_round_metrics()

    first["candidates"] = 10

    assert second["candidates"] == 0


def test_keyword_rotation_is_stable_and_round_dependent() -> None:
    keywords = ["别墅软装公司", "别墅全案设计公司", "高端私宅设计公司"]

    assert rotate_keywords(keywords, 1) == keywords
    assert rotate_keywords(keywords, 2) == keywords[1:] + keywords[:1]
    assert rotate_keywords(keywords, 4) == keywords
    assert rotate_keywords([], 3) == []


def test_verified_erp_eligibility_requires_mainland_mobile() -> None:
    lead = _eligible_lead()

    assert verified_erp_ineligibility(lead) is None
    assert verified_erp_ineligibility({**lead, "mobile": None}) == "missing_mobile"
    assert (
        verified_erp_ineligibility({**lead, "mobile": "020-12345678"})
        == "missing_mobile"
    )


def test_verified_delivery_eligibility_rejects_invalid_email_without_mobile() -> None:
    lead = {
        **_eligible_lead(),
        "mobile": None,
        "email": "not-an-email",
    }

    assert verified_delivery_ineligibility(lead) == "missing_mobile_or_email"


def test_verified_delivery_eligibility_accepts_one_of_two_selected_emails() -> None:
    lead = {
        **_eligible_lead(),
        "mobile": None,
        "email": "info@company.cn; contact@company.cn",
    }

    assert verified_delivery_ineligibility(lead) is None


def test_verified_erp_eligibility_requires_company_name_evidence() -> None:
    lead = _eligible_lead()

    assert verified_erp_ineligibility(
        {**lead, "company_name_verification_status": "verified_public_web"}
    ) is None

    assert (
        verified_erp_ineligibility(
            {**lead, "company_name_verification_status": "unverified"}
        )
        == "company_name_unverified"
    )
    assert (
        verified_erp_ineligibility(
            {**lead, "company_name_verification_status": "verified_registry"}
        )
        == "company_name_unverified"
    )


def test_verified_erp_eligibility_requires_public_source_url() -> None:
    lead = _eligible_lead()

    assert (
        verified_erp_ineligibility({**lead, "contact_evidence_url": None})
        == "missing_source_url"
    )
    assert (
        verified_erp_ineligibility({**lead, "contact_evidence_url": "豆包"})
        == "missing_source_url"
    )


def test_verified_erp_eligibility_requires_verified_contact_evidence() -> None:
    lead = _eligible_lead()

    assert (
        verified_erp_ineligibility(
            {**lead, "contact_verification_status": "unverified"}
        )
        == "contact_unverified"
    )
    assert verified_erp_ineligibility(
        {
            **lead,
            "contact_verification_status": "verified_cross_source",
            "company_name_verification_status": "verified_cross_source",
        }
    ) is None
    for status in ("pending_login_source", "unverified_model_only", "rejected_mismatch"):
        assert (
            verified_erp_ineligibility(
                {**lead, "contact_verification_status": status}
            )
            == "contact_unverified"
        )
    assert (
        verified_erp_ineligibility(
            {**lead, "contact_verification_status": "rejected"}
        )
        == "contact_unverified"
    )


def test_verified_erp_eligibility_requires_matching_verification() -> None:
    lead = {
        **_eligible_lead(),
        "contact_verification_status": "verified_cross_source",
    }

    assert verified_erp_ineligibility(
        {**lead, "verification_status": "cross_verified"}
    ) is None

    assert (
        verified_erp_ineligibility(
            {**lead, "verification_status": "model_consensus_match"}
        )
        is None
    )
    assert (
        verified_erp_ineligibility({**lead, "verification_status": "model_disagreement"})
        == "verification_not_matched"
    )
    assert (
        verified_erp_ineligibility({**lead, "verification_status": "verification_failed"})
        == "verification_not_matched"
    )


def test_official_page_contact_evidence_does_not_require_model_match() -> None:
    lead = _eligible_lead()

    assert (
        verified_erp_ineligibility(
            {**lead, "verification_status": "single_model_reject"}
        )
        is None
    )


def test_public_web_contact_evidence_is_not_rejected_by_model_verification() -> None:
    lead = {
        **_eligible_lead(),
        "company_name_verification_status": "verified_public_web",
        "contact_verification_status": "verified_public_web",
        "verification_status": "verification_failed",
    }

    assert verified_erp_ineligibility(lead) is None
    assert strict_verified_lead_count([lead]) == 1


def test_continuous_round_enters_cooldown_and_can_pause(tmp_path: Path) -> None:
    clock = FakeClock()
    deepseek = RoundVerifier("deepseek")
    doubao = RoundVerifier("doubao")
    service: CollectionService
    task_id = ""

    def fake_sleep(seconds: float) -> None:
        clock.advance(seconds)
        service.pause_task(task_id)

    service = CollectionService(
        TaskStore(tmp_path),
        {},
        {"deepseek": deepseek, "doubao": doubao},
        now=clock.now,
        sleep=fake_sleep,
        max_concurrent_tasks=1,
    )
    task_id = service.create_task(_payload())["id"]
    try:
        service.start_task(task_id)
        paused = _wait_for_status(service, task_id, "paused")

        assert paused["round_number"] == 1
        assert paused["round_phase"] == "cooldown"
        assert paused["round_metrics"]["new_companies"] == 1
        assert deepseek.discovery_calls == 4
    finally:
        service.shutdown()


def test_continuous_task_advances_round_deduplicates_and_can_terminate(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    deepseek = RoundVerifier("deepseek")
    doubao = RoundVerifier("doubao")
    service: CollectionService
    task_id = ""

    def fake_sleep(seconds: float) -> None:
        clock.advance(seconds)
        if deepseek.discovery_calls >= 8:
            service.terminate_task(task_id)

    service = CollectionService(
        TaskStore(tmp_path),
        {},
        {"deepseek": deepseek, "doubao": doubao},
        now=clock.now,
        sleep=fake_sleep,
        max_concurrent_tasks=1,
    )
    task_id = service.create_task(_payload())["id"]
    try:
        service.start_task(task_id)
        terminated = _wait_for_status(service, task_id, "terminated")

        assert terminated["round_number"] == 2
        assert terminated["round_phase"] == "cooldown"
        assert terminated["lead_count"] == 1
        assert deepseek.discovery_calls == 8
    finally:
        service.shutdown()


def test_continuous_task_completes_when_strict_verified_target_is_reached(
    tmp_path: Path,
) -> None:
    service = CollectionService(
        TaskStore(tmp_path),
        {},
        {"deepseek": RoundVerifier("deepseek"), "doubao": RoundVerifier("doubao")},
        company_name_verifier=StrictCompanyVerifier(),
        contact_verifier=StrictContactVerifier(),
        max_concurrent_tasks=1,
    )
    task_id = service.create_task({**_payload(), "limit_per_city": 1})["id"]
    try:
        service.prepare_start(task_id)
        service.run_task(task_id)
        completed = service.get_task(task_id)

        assert completed["status"] == "completed"
        assert completed["round_phase"] == "completed"
        assert completed["next_round_at"] is None
        assert CollectionService.task_summary(completed)["contact_lead_count"] == 1
    finally:
        service.shutdown()


def test_continuous_task_does_not_start_another_round_after_target_is_reached(
    tmp_path: Path,
) -> None:
    verifier = RoundVerifier("doubao")
    service = CollectionService(
        TaskStore(tmp_path),
        {},
        {"doubao": verifier},
        max_concurrent_tasks=1,
    )
    task_id = service.create_task(
        {
            **_payload(),
            "models": ["doubao"],
            "limit_per_city": 1,
        }
    )["id"]
    try:
        service.store.update(
            task_id,
            lambda task: task.update(
                status="running",
                leads=[{**_eligible_lead(), "city": task["cities"][0]}],
            ),
        )

        service.run_task(task_id)
        completed = service.get_task(task_id)

        assert completed["status"] == "completed"
        assert completed["round_phase"] == "completed"
        assert verifier.discovery_calls == 0
    finally:
        service.shutdown()
