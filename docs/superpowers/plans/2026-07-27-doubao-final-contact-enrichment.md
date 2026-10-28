# Doubao Final Contact Enrichment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ensure every candidate company is queried individually through Doubao for final contact details, without DeepSeek fallback, and allow existing candidates to be re-enriched.

**Architecture:** Candidate discovery remains driven by the models selected in the task. `CollectionService` uses a dedicated Doubao-only enrichment path for all candidates, and a re-enrichment API resets existing leads to pending before reusing the same two-stage runner without rediscovering companies.

**Tech Stack:** Python 3.11, FastAPI, pytest, Pydantic, existing JSON `TaskStore`.

## Global Constraints

- Candidate discovery may use Doubao, DeepSeek, or both.
- Final contact enrichment must use Doubao only.
- Doubao failures must not fall back to DeepSeek.
- Missing public fields remain empty and display as “未公开”.
- Existing candidates must be re-enriched without rediscovery.
- API keys must never be printed or committed.

---

### Task 1: Make final enrichment Doubao-only

**Files:**
- Modify: `backend/tests/collection/test_two_stage_service.py`
- Modify: `backend/app/collection/service.py:671`

**Interfaces:**
- Consumes: `CollectionService.verifiers: dict[str, ModelVerifier]` and `ModelVerifier.enrich_company(...)`.
- Produces: `CollectionService._enrich_with_doubao(task, company) -> tuple[dict | None, str | None, str | None]`.

- [ ] **Step 1: Write the failing tests**

Replace the fallback test with assertions that a selected DeepSeek discovery model is never used for enrichment when Doubao is available, and that Doubao failure does not call DeepSeek:

```python
def test_enrichment_always_uses_doubao_even_when_deepseek_is_selected_first(tmp_path: Path) -> None:
    store = RecordingStore(tmp_path)
    deepseek = TwoStageVerifier()
    doubao = TwoStageVerifier()
    doubao.id = "doubao"
    service = CollectionService(store, {"web_search": EmptyProvider()}, {"deepseek": deepseek, "doubao": doubao})
    task = service.create_task(_payload(["deepseek", "doubao"]))

    service.prepare_start(task["id"])
    service.run_task(task["id"])
    completed = service.get_task(task["id"])

    assert deepseek.discovery_calls == [("武汉市", ["软装公司", "整装公司"])]
    assert deepseek.enrichment_calls == []
    assert len(doubao.enrichment_calls) == 4
    assert {lead["enrichment_model"] for lead in completed["leads"]} == {"doubao"}


def test_doubao_enrichment_failure_never_falls_back_to_deepseek(tmp_path: Path) -> None:
    store = RecordingStore(tmp_path)
    deepseek = TwoStageVerifier()
    service = CollectionService(store, {"web_search": EmptyProvider()}, {"deepseek": deepseek, "doubao": FailingEnricher()})
    task = service.create_task(_payload(["deepseek", "doubao"]))

    service.prepare_start(task["id"])
    service.run_task(task["id"])
    completed = service.get_task(task["id"])

    assert deepseek.enrichment_calls == []
    assert all(lead["enrichment_state"] == "failed" for lead in completed["leads"])
    assert all("doubao" in lead["enrichment_error"] for lead in completed["leads"])
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/collection/test_two_stage_service.py -q
```

Expected: the first test reports DeepSeek enrichment calls and the second reports completed fallback enrichment.

- [ ] **Step 3: Implement the minimal Doubao-only path**

Rename `_enrich_with_fallback` to `_enrich_with_doubao`, call it from `_enrich_city_candidates`, and replace the model loop with a single Doubao verifier lookup:

```python
def _enrich_with_doubao(
    self,
    task: dict[str, Any],
    company: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    model_id = "doubao"
    verifier = self.verifiers.get(model_id)
    enrich = getattr(verifier, "enrich_company", None)
    if not callable(enrich):
        return None, None, "doubao: enrichment model unavailable"
    errors: list[str] = []
    for _ in range(2):
        try:
            return (
                enrich(
                    request_text=task["request_text"],
                    city=company["city"],
                    company=company,
                    required_contacts=task.get("required_contacts") or ["mobile"],
                ),
                model_id,
                None,
            )
        except (ModelVerificationError, TypeError, ValueError) as exc:
            errors.append(f"doubao: {exc}")
    return None, None, "; ".join(errors)
```

- [ ] **Step 4: Run focused and regression tests**

Run:

```bash
conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/collection/test_two_stage_service.py backend/tests/api/test_collection_mvp.py -q
conda run -n doubao-leads --no-capture-output python -m ruff check backend/app backend/tests
```

Expected: all tests pass and Ruff reports no errors.

- [ ] **Step 5: Commit**

```bash
git add backend/app/collection/service.py backend/tests/collection/test_two_stage_service.py
git commit -m "fix: require doubao for contact enrichment"
```

### Task 2: Re-enrich existing candidates without rediscovery

**Files:**
- Modify: `backend/tests/api/test_collection_mvp.py`
- Modify: `backend/app/collection/service.py:180`
- Modify: `backend/app/api/routes/collection_tasks.py:189`

**Interfaces:**
- Produces: `CollectionService.prepare_contact_enrichment(task_id) -> tuple[dict, bool]`.
- Produces: `POST /api/v1/collection-tasks/{task_id}/enrich-contacts` returning a task summary with HTTP 202.

- [ ] **Step 1: Write the failing API test**

Create a completed task with one existing DeepSeek-enriched lead, call the endpoint, and assert that the company discovery call count does not increase while Doubao enrichment runs and overwrites `enrichment_model` with `doubao`.

```python
response = client.post(f"/api/v1/collection-tasks/{task_id}/enrich-contacts")
assert response.status_code == 202
lead = client.get(f"/api/v1/collection-tasks/{task_id}/leads").json()["items"][0]
assert lead["enrichment_model"] == "doubao"
```

- [ ] **Step 2: Run the API test and verify RED**

Run:

```bash
conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/api/test_collection_mvp.py -q
```

Expected: FAIL with HTTP 404 because the endpoint does not exist.

- [ ] **Step 3: Implement task reset and route**

Add `prepare_contact_enrichment` that returns `(task, False)` while running, otherwise resets every existing lead to `pending`, clears `enrichment_model` and `enrichment_error`, sets `enrichment_completed` to zero, sets status to `running`, and saves an accepted `reenrich_contacts` activity entry. Preserve `completed_city_discovery` so `run_task` skips candidate discovery.

Add the route:

```python
@router.post("/collection-tasks/{task_id}/enrich-contacts", status_code=status.HTTP_202_ACCEPTED)
def enrich_collection_contacts(task_id: str, request: Request, background_tasks: BackgroundTasks) -> dict:
    service = _service(request)
    try:
        task, should_run = service.prepare_contact_enrichment(task_id)
    except TaskNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="collection task not found") from exc
    if should_run:
        background_tasks.add_task(service.run_task, task_id)
    return service.task_summary(task)
```

- [ ] **Step 4: Run backend verification**

Run:

```bash
conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/collection/test_two_stage_service.py backend/tests/api/test_collection_mvp.py -q
conda run -n doubao-leads --no-capture-output python -m ruff check backend/app backend/tests
```

Expected: all tests pass and Ruff reports no errors.

- [ ] **Step 5: Commit**

```bash
git add backend/app/collection/service.py backend/app/api/routes/collection_tasks.py backend/tests/api/test_collection_mvp.py
git commit -m "feat: re-enrich existing contacts with doubao"
```

### Task 3: Runtime verification and Guangzhou re-enrichment

**Files:**
- Runtime data only: `data/tasks/a3b663ed-60f4-4e4e-8311-d837247e124d.json`

**Interfaces:**
- Consumes: `POST /api/v1/collection-tasks/{task_id}/enrich-contacts`.

- [ ] **Step 1: Restart the backend in the `doubao-leads` environment**

Start `uvicorn app.main:create_app --factory --app-dir backend --host 127.0.0.1 --port 8010` and verify `/api/v1/models` reports Doubao configured without exposing the key.

- [ ] **Step 2: Verify one real Doubao enrichment**

Call `enrich_company` for one known candidate and assert the response is structurally valid and `enrichment_model` is Doubao. Empty contact fields are acceptable; fabricated fallback values are not.

- [ ] **Step 3: Re-enrich the existing Guangzhou task**

Call:

```text
POST /api/v1/collection-tasks/a3b663ed-60f4-4e4e-8311-d837247e124d/enrich-contacts
```

Poll the task until completion. Confirm all successful records have `enrichment_model=doubao` and no record has `enrichment_model=deepseek`.

- [ ] **Step 4: Run final Git checks and push**

```bash
git status --short
git push origin codex/five-city-lead-platform:main
```

Expected: only runtime data ignored by Git, and the remote branch receives all commits.
