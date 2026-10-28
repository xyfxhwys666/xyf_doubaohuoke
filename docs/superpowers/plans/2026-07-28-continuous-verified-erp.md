# Continuous Verified ERP Collection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a user-controlled continuous collection mode that runs 100-candidate rounds with 10-minute cooldowns and only sends mobile-bearing, publicly sourced, model-verified, deduplicated leads to ERP.

**Architecture:** Keep the existing `CollectionService`, two-worker FIFO scheduler, task store, verification consensus, and ERP idempotency. Add a focused continuous-round policy module, persist round state in the existing task record, and make the scheduler worker loop cooperatively through collection, verification, safe delivery, and cooldown. The frontend keeps the current model selector and adds two explicit opt-in toggles plus round metrics.

**Tech Stack:** Python 3, FastAPI, Pydantic, pytest, React, TypeScript, Vite, existing JSON/PostgreSQL task stores, existing ERP delivery client.

## Global Constraints

- Keep the existing model selector, default state, configured labels, and disabled unconfigured models unchanged.
- User-selected models continue to control candidate discovery and verification only.
- Final per-company contact enrichment always uses Doubao exactly as in the previous version, with no fallback to another model.
- `continuous_mode` and `verified_auto_deliver` default to `false`; legacy tasks inherit `false`.
- Each continuous round discovers at most 100 candidates and then cools down for exactly 600 seconds.
- A continuous task stops only after a cooperative pause or terminate request.
- ERP eligibility requires a mainland mobile, an HTTP(S) public source URL, and `single_model_match` or `model_consensus_match`.
- Failed, rejected, disputed, unverifiable, missing-mobile, and missing-source leads remain local and are never automatically delivered.
- Existing ERP GUID and fingerprint idempotency remains the final duplicate-send guard.
- No model, company, contact, source, or test fixture may be fabricated into production results.

---

### Task 1: Continuous Round Policy and ERP Eligibility

**Files:**
- Create: `backend/app/collection/continuous.py`
- Create: `backend/tests/collection/test_continuous_collection.py`

**Interfaces:**
- Produces: `ROUND_CANDIDATE_LIMIT = 100`, `ROUND_COOLDOWN_SECONDS = 600`.
- Produces: `new_round_metrics() -> dict[str, int]`.
- Produces: `rotate_keywords(keywords: list[str], round_number: int) -> list[str]`.
- Produces: `verified_erp_ineligibility(lead: dict[str, Any]) -> str | None`.

- [ ] **Step 1: Write failing policy tests**

```python
def test_verified_erp_eligibility_requires_mobile_source_and_match() -> None:
    eligible = {"mobile": "13800138000", "source_url": "https://example.cn/company", "verification_status": "single_model_match"}
    assert verified_erp_ineligibility(eligible) is None
    assert verified_erp_ineligibility({**eligible, "mobile": None}) == "missing_mobile"
    assert verified_erp_ineligibility({**eligible, "source_url": None}) == "missing_source_url"
    assert verified_erp_ineligibility({**eligible, "verification_status": "model_disagreement"}) == "verification_not_matched"

def test_keyword_rotation_is_stable_and_round_dependent() -> None:
    keywords = ["别墅软装公司", "别墅全案设计公司", "高端私宅设计公司"]
    assert rotate_keywords(keywords, 1) == keywords
    assert rotate_keywords(keywords, 2) == keywords[1:] + keywords[:1]
```

- [ ] **Step 2: Run the tests and observe the missing module failure**

Run: `conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/collection/test_continuous_collection.py -q`

Expected: collection fails with `ModuleNotFoundError: No module named 'app.collection.continuous'`.

- [ ] **Step 3: Implement the policy module**

```python
ROUND_CANDIDATE_LIMIT = 100
ROUND_COOLDOWN_SECONDS = 600
MATCHED_VERIFICATION_STATUSES = {"single_model_match", "model_consensus_match"}
MOBILE_PATTERN = re.compile(r"^1[3-9]\d{9}$")

def new_round_metrics() -> dict[str, int]:
    return {"candidates": 0, "new_companies": 0, "verification_matches": 0,
            "verification_rejects": 0, "erp_succeeded": 0, "erp_failed": 0}

def rotate_keywords(keywords: list[str], round_number: int) -> list[str]:
    if not keywords:
        return []
    offset = (max(1, round_number) - 1) % len(keywords)
    return keywords[offset:] + keywords[:offset]

def verified_erp_ineligibility(lead: dict[str, Any]) -> str | None:
    mobile = re.sub(r"\D", "", str(lead.get("mobile") or ""))
    if not MOBILE_PATTERN.fullmatch(mobile):
        return "missing_mobile"
    source_url = str(lead.get("source_url") or "").strip()
    if urlparse(source_url).scheme not in {"http", "https"} or not urlparse(source_url).netloc:
        return "missing_source_url"
    if lead.get("verification_status") not in MATCHED_VERIFICATION_STATUSES:
        return "verification_not_matched"
    return None
```

- [ ] **Step 4: Run focused tests and commit**

Run: `conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/collection/test_continuous_collection.py -q`

Expected: all Task 1 tests pass.

Commit: `git commit -m "feat: add continuous collection policy"`

---

### Task 2: Persisted Task Contract and API Validation

**Files:**
- Modify: `backend/app/collection/service.py:33-56,297-337,427-453`
- Modify: `backend/app/api/routes/collection_tasks.py:26-90,189-239`
- Modify: `backend/tests/api/test_collection_tasks.py`
- Modify: `backend/tests/collection/test_task_scheduler.py`

**Interfaces:**
- Consumes: `new_round_metrics()` from Task 1.
- Produces task fields: `continuous_mode`, `verified_auto_deliver`, `round_number`, `round_phase`, `round_started_at`, `next_round_at`, `round_metrics`, `round_checkpoint`.
- Produces natural-language request fields: `continuous_mode: bool = False`, `verified_auto_deliver: bool = False`.

- [ ] **Step 1: Add failing API and default-migration tests**

```python
def test_natural_language_task_persists_continuous_safe_delivery(client) -> None:
    response = client.post("/api/v1/collection-tasks/from-natural-language", json={
        "request_text": "查深圳别墅软装公司，需要手机号",
        "models": ["deepseek"],
        "continuous_mode": True,
        "verified_auto_deliver": True,
    })
    assert response.status_code == 201
    body = response.json()
    assert body["continuous_mode"] is True
    assert body["verified_auto_deliver"] is True
    assert body["auto_deliver"] is False
    assert body["round_number"] == 1

def test_legacy_task_defaults_to_non_continuous_display_only(store) -> None:
    store.save({"id": "legacy", "status": "draft", "leads": []})
    task = store.load("legacy")
    assert task["continuous_mode"] is False
    assert task["verified_auto_deliver"] is False
```

Also assert `422` when no model is selected, when any selected model is unconfigured, when both `auto_deliver` and `verified_auto_deliver` are true, or when verified delivery is enabled without ERP configuration.

- [ ] **Step 2: Run focused tests and confirm field/assertion failures**

Run: `conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/api/test_collection_tasks.py backend/tests/collection/test_task_scheduler.py -q`

Expected: new assertions fail because continuous fields are absent.

- [ ] **Step 3: Add defaults and request validation**

```python
TASK_DEFAULTS.update({
    "continuous_mode": False,
    "verified_auto_deliver": False,
    "round_number": 1,
    "round_phase": None,
    "round_started_at": None,
    "next_round_at": None,
})

def _with_task_defaults(task: dict[str, Any]) -> dict[str, Any]:
    # Apply scalar defaults first, then allocate fresh mutable round state per task.
    for key, value in TASK_DEFAULTS.items():
        task.setdefault(key, value)
    task.setdefault("round_metrics", new_round_metrics())
    task.setdefault("round_checkpoint", {})
    return task

class NaturalLanguageCollectionTaskRequest(BaseModel):
    request_text: str = Field(min_length=1, max_length=4000)
    models: list[str] = Field(min_length=1)
    auto_deliver: bool = False
    continuous_mode: bool = False
    verified_auto_deliver: bool = False

    @model_validator(mode="after")
    def validate_delivery_mode(self):
        if self.auto_deliver and self.verified_auto_deliver:
            raise ValueError("automatic delivery modes are mutually exclusive")
        return self
```

Use a fresh `new_round_metrics()` per task rather than sharing the mutable defaults dictionary. Copy the exact user-provided `models` list into the task and never alter it during intent parsing, round changes, or recovery.

- [ ] **Step 4: Run focused tests and commit**

Expected: API and scheduler default tests pass.

Commit: `git commit -m "feat: persist continuous task options"`

---

### Task 3: Deliver Only Verified Leads Without Changing Doubao Enrichment

**Files:**
- Modify: `backend/app/collection/service.py:569-642,1274-1351`
- Modify: `backend/tests/collection/test_two_stage_service.py`
- Modify: `backend/tests/collection/test_continuous_collection.py`

**Interfaces:**
- Consumes: `verified_erp_ineligibility(lead)` from Task 1.
- Produces: `_deliver_verified_lead(task, lead)` and `_deliver_verified_pending_leads(task)`.

- [ ] **Step 1: Preserve Doubao enrichment and add failing safe-delivery tests**

```python
def test_enrichment_still_uses_doubao_when_deepseek_is_selected(tmp_path: Path) -> None:
    service, deepseek, doubao = make_service_with_spies(tmp_path)
    task = service.create_task({**_payload(["deepseek"]), "limit_per_city": 1})
    service.run_task(task["id"])
    assert deepseek.enrich_calls == 0
    assert doubao.enrich_calls == 1

def test_verified_delivery_rechecks_every_gate_before_erp(tmp_path: Path) -> None:
    service, delivery = make_verified_delivery_service(tmp_path)
    leads = [eligible_lead(), {**eligible_lead(), "mobile": None},
             {**eligible_lead(), "source_url": None},
             {**eligible_lead(), "verification_status": "model_disagreement"}]
    for lead in leads:
        service._deliver_verified_lead(task, lead)
    assert [call["company_name"] for call in delivery.calls] == [eligible_lead()["company_name"]]
```

- [ ] **Step 2: Run the tests and observe that verified delivery has no entry point**

Run: `conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/collection/test_two_stage_service.py backend/tests/collection/test_continuous_collection.py -q`

Expected: the existing Doubao enrichment test passes and the new safe-delivery test fails because the verified path is absent.

- [ ] **Step 3: Split ERP sending from eligibility and add the verified path**

Keep legacy `_deliver_lead` behavior for legacy `auto_deliver`. Extract its state transitions and actual `lead_delivery.deliver()` call into `_send_lead`. Add:

```python
def _deliver_verified_lead(self, task, lead) -> None:
    reason = verified_erp_ineligibility(lead)
    if not task.get("verified_auto_deliver") or reason:
        if lead.get("delivery_status") not in {"succeeded", "failed"}:
            lead.update(delivery_status="display_only", delivery_error=reason)
            self.store.save_leads(task["id"], [lead])
        return
    self._send_lead(task, lead)
```

Repeat the eligibility check immediately before every retry. Preserve the existing stable fingerprint/GUID handling and never reset `succeeded` leads to pending.

- [ ] **Step 4: Run focused tests and commit**

Expected: Doubao-only enrichment, verification gate, persistence-before-delivery, ERP failure continuation, and idempotency tests pass.

Commit: `git commit -m "feat: gate ERP delivery on model verification"`

---

### Task 4: Continuous Round Loop, Cooldown, Pause, and Recovery

**Files:**
- Modify: `backend/app/collection/service.py:208-274,644-711,932-1086`
- Modify: `backend/app/collection/models.py:33-49,180-305`
- Modify: `backend/tests/collection/test_continuous_collection.py`
- Modify: `backend/tests/collection/test_task_scheduler.py`

**Interfaces:**
- Consumes: Task 1 constants and rotation helper; Task 3 verified delivery methods.
- Produces: `_run_continuous_task(task_id)`, `_complete_round(task_id)`, `_wait_for_next_round(task_id)`, `_prepare_next_round(task_id)`.
- Extends `ModelVerifier.discover_companies(*, request_text: str, city: str, keywords: list[str], limit: int = 100, excluded_company_names: list[str] | None = None) -> list[dict[str, Any]]` so each round can ask only the user-selected models for companies not already seen.
- `CollectionService.__init__` gains optional `now: Callable[[], datetime]` and `sleep: Callable[[float], None]` test seams with production defaults.

- [ ] **Step 1: Add fake-clock failing tests**

Add `FakeClock`, whose `now()` returns a stored UTC datetime and whose `sleep(seconds)` advances that datetime before invoking an optional callback. Add `_continuous_service(tmp_path, clock, discoveries)` returning a service with a verifier that emits one supplied discovery list per call and a recording ERP delivery. Then cover these exact behaviors:

```python
@dataclass
class FakeClock:
    value: datetime = datetime(2026, 7, 28, tzinfo=UTC)
    on_sleep: Callable[[], None] | None = None

    def now(self) -> datetime:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)
        if self.on_sleep:
            self.on_sleep()

def test_continuous_round_caps_discovery_and_enters_ten_minute_cooldown(tmp_path: Path) -> None:
    clock = FakeClock()
    service, verifier, _ = _continuous_service(tmp_path, clock, [make_candidates(130)])
    task = service.create_task({**_payload(["deepseek"]), "continuous_mode": True})
    clock.on_sleep = lambda: service.pause_task(task["id"])
    service.start_task(task["id"])
    assert _wait_for_status(service, task["id"], "paused")
    saved = service.get_task(task["id"])
    assert saved["round_metrics"]["candidates"] == 100
    assert datetime.fromisoformat(saved["next_round_at"]) - datetime.fromisoformat(saved["round_started_at"]) >= timedelta(seconds=600)
    assert verifier.discovery_calls == 1

def test_zero_new_round_advances_and_pause_during_cooldown_is_cooperative(tmp_path: Path) -> None:
    clock = FakeClock()
    service, verifier, _ = _continuous_service(tmp_path, clock, [[], []])
    task = service.create_task({**_payload(["deepseek"]), "continuous_mode": True})
    clock.on_sleep = lambda: service.pause_task(task["id"]) if service.get_task(task["id"])["round_number"] == 2 else None
    service.start_task(task["id"])
    assert _wait_for_status(service, task["id"], "paused")
    saved = service.get_task(task["id"])
    assert saved["round_number"] == 2
    assert saved["round_phase"] == "cooldown"
    assert verifier.discovery_calls == 2

def test_restart_keeps_history_and_never_resends_successful_guid(tmp_path: Path) -> None:
    clock = FakeClock()
    service, _, delivery = _continuous_service(tmp_path, clock, [[eligible_candidate()], [eligible_candidate()]])
    task = service.create_task({**_payload(["deepseek"]), "continuous_mode": True, "verified_auto_deliver": True})
    clock.on_sleep = lambda: service.pause_task(task["id"])
    service.start_task(task["id"])
    assert _wait_for_status(service, task["id"], "paused")
    first_guid = delivery.guids[0]
    service.shutdown()
    recovered = _continuous_service_from_existing_store(tmp_path, clock, delivery)
    recovered.resume_task(task["id"])
    clock.on_sleep = lambda: recovered.terminate_task(task["id"])
    assert _wait_for_status(recovered, task["id"], "terminated")
    assert recovered.get_task(task["id"])["lead_count"] == 1
    assert delivery.guids.count(first_guid) == 1
```

Use a fake `now()` and a `sleep()` callback that advances time, and request pause/terminate from the callback so tests finish immediately without a real 600-second wait.

- [ ] **Step 2: Run focused tests and observe one-shot completion failures**

Run: `conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/collection/test_continuous_collection.py backend/tests/collection/test_task_scheduler.py -q`

Expected: continuous tasks currently become `completed` after one pass.

- [ ] **Step 3: Add the round loop**

```python
def _run_continuous_task(self, task_id: str) -> None:
    while True:
        self._control_checkpoint(task_id)
        task = self.store.load(task_id)
        self._prepare_round_if_needed(task)
        self._run_two_stage_task(task, finalize=False, round_limit=ROUND_CANDIDATE_LIMIT)
        self._verify_task_locked(task_id)
        task = self.store.load(task_id)
        self._deliver_verified_pending_leads(task)
        self._complete_round(task_id)
        self._wait_for_next_round(task_id)
        self._prepare_next_round(task_id)
```

At round start, rotate keywords for model discovery, pass at most 300 recent historical company names through `excluded_company_names`, and retain the original user keywords in task metadata. When the exclusion list is non-empty, append a `不得返回以下已采集公司` section containing those names to the model prompt. Limit the sum of accepted candidates across all cities to 100. Historical normalization and deduplication remain authoritative.

- [ ] **Step 4: Persist phase and metrics at every boundary**

Set `round_phase` before discovery, enrichment, verification, delivery, and cooldown. Compute `next_round_at = now + timedelta(seconds=600)`. During cooldown, sleep in at most one-second increments and call `_control_checkpoint` between waits. A restart requeues `running` tasks and uses the stored `next_round_at`; it never starts a fresh cooldown from zero.

- [ ] **Step 5: Keep continuous tasks active without corrupting one-shot status**

Change the scheduled-run completion block so it only sets `completed_at` for non-continuous terminal tasks. Continuous tasks remain `running` during cooldown. Existing one-shot tasks still set `completed` or `completed_with_errors` exactly as before.

- [ ] **Step 6: Run focused and full backend tests, then commit**

Run focused tests first, then:

`conda run -n doubao-leads --no-capture-output python -m pytest backend/tests -q`

Expected: all backend tests pass; no test waits 600 real seconds.

Commit: `git commit -m "feat: run persistent collection rounds"`

---

### Task 5: Frontend Opt-In Controls and Round Status

**Files:**
- Modify: `frontend/src/types.ts:16-50`
- Modify: `frontend/src/api.ts:23-30`
- Modify: `frontend/src/App.tsx:6-149`
- Modify: `frontend/src/styles.css`

**Interfaces:**
- Consumes API fields added in Task 2 and round state produced in Task 4.
- Produces request body with the exact selected `models`, `continuous_mode`, and `verified_auto_deliver` values.

- [ ] **Step 1: Add frontend type/build failure first**

Extend `CollectionTask` with typed round fields and update the API call signature before updating `App.tsx`, then run the build to observe call-site/type failures.

Run: `conda run -n doubao-leads --no-capture-output npm.cmd --prefix frontend run build`

Expected: TypeScript reports the old `createNaturalLanguageTask` signature or missing component state.

- [ ] **Step 2: Preserve the current model selector without behavioral changes**

Keep the existing model cards, ordering, multi-select behavior, configured labels, disabled unconfigured cards, and current default selection logic byte-for-byte unless a type-only adjustment is required. Do not connect either new toggle to `selectedModels`. Doubao contact enrichment remains a backend pipeline rule and does not add Doubao to the displayed verification selection.

- [ ] **Step 3: Add two default-off toggles and submit exact values**

```tsx
const [continuousMode, setContinuousMode] = useState(false)
const [verifiedAutoDeliver, setVerifiedAutoDeliver] = useState(false)

await api.createNaturalLanguageTask(
  requestText.trim(), selectedModels, continuousMode, verifiedAutoDeliver,
)
```

Label the controls `持续采集` and `验证通过后自动推送 ERP`. Disable only the verified-delivery toggle when ERP is unconfigured. Do not alter selected models when either toggle changes.

- [ ] **Step 4: Show round state without changing the compact work UI**

Add current round, phase, per-round candidate/new/match/reject/ERP counts, and formatted `next_round_at` to the existing progress panel. Reuse existing status tags and compact rows; do not introduce nested cards. Continue showing row-level verification and delivery status.

- [ ] **Step 5: Build and commit**

Run: `conda run -n doubao-leads --no-capture-output npm.cmd --prefix frontend run build`

Expected: Vite production build succeeds with no TypeScript errors.

Commit: `git commit -m "feat: add continuous verified delivery controls"`

---

### Task 6: End-to-End Verification, Browser QA, and Release

**Files:**
- Modify only files required to fix failures found by verification.

**Interfaces:**
- Verifies all interfaces from Tasks 1-5 and existing account, export, manual API, scheduler, and ERP contracts.

- [ ] **Step 1: Run formatting and full automated tests**

```text
conda run -n doubao-leads --no-capture-output python -m ruff check backend
conda run -n doubao-leads --no-capture-output python -m pytest backend/tests -q
conda run -n doubao-leads --no-capture-output npm.cmd --prefix frontend run build
```

Expected: Ruff, all backend tests, and the frontend production build pass.

- [ ] **Step 2: Restart local backend and frontend using the project conda environment**

Use the existing project restart scripts/process commands. Verify `http://127.0.0.1:8010/health` returns success and `http://127.0.0.1:5173/` loads.

- [ ] **Step 3: Browser-test the actual workflow**

Verify at desktop and mobile widths:

- model cards, current default selection, and unconfigured grey states remain unchanged;
- single and multiple configured model selections remain intact after task creation;
- both new toggles default off;
- a continuous test task shows round 1 and its phase;
- pause, resume, and terminate controls work during a fake/short test cooldown configuration;
- no controls overlap and the results table still renders source, verification, and ERP state.

- [ ] **Step 4: Run a no-send safety smoke test**

Use a fake ERP delivery client and prove that eligible data sends once while missing-mobile, missing-source, rejected, disputed, and failed-verification rows send zero times. Confirm restart does not resend the successful GUID.

- [ ] **Step 5: Review the final diff and push**

Confirm no `.env`, API key, ERP URL, token, `.planning`, pytest cache, or generated export is staged. Commit any final verification fixes, then push `codex/five-city-lead-platform` to `origin`.

Expected: Gitee branch contains the feature commits, local services are healthy, and the UI is available at `http://127.0.0.1:5173/`.
