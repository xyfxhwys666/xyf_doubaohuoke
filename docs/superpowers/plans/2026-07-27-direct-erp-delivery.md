# Direct ERP Delivery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the lead collector without PostgreSQL or Redis, persist task state locally, and push each qualified lead to ERP with durable failure retry.

**Architecture:** FastAPI keeps SQLAlchemy only for account data in an embedded SQLite file and uses the existing `TaskStore` JSON implementation for collection tasks and leads. A focused `ErpLeadDelivery` component maps one lead to the ERP contract, posts it immediately, records successful fingerprints, and stores failed deliveries in an atomic JSON queue. `CollectionService` invokes the component after a lead has been persisted so collection continues even when ERP is unavailable.

**Tech Stack:** Python 3.12, FastAPI, Pydantic Settings, SQLAlchemy/SQLite, httpx, pytest, UTF-8 JSON.

## Global Constraints

- PostgreSQL, Redis, Celery, and Docker services must not be required to start or use the product.
- ERP business success means HTTP success and response JSON `errCode == 0`.
- Do not log API keys, bearer tokens, or complete authorization headers.
- Persist a lead locally before attempting ERP delivery.
- A failed ERP delivery must never stop collection of later companies.
- Keep CSV, XLSX, JSON download and existing natural-language collection behavior unchanged.

---

### Task 1: Restore the Lightweight Runtime

**Files:**
- Modify: `backend/app/main.py`
- Modify: `backend/app/config.py`
- Modify: `backend/app/db.py`
- Modify: `backend/tests/conftest.py`
- Test: `backend/tests/test_health.py`

**Interfaces:**
- Consumes: `TaskStore(data_dir: Path)` and `CollectionService(store, providers, verifiers)`.
- Produces: `create_app(settings)` using JSON collection storage and an embedded SQLite account store.

- [ ] **Step 1: Write the failing startup test**

```python
def test_app_starts_with_sqlite_and_json_task_store(tmp_path):
    settings = build_test_settings(
        tmp_path,
        database_url=f"sqlite:///{tmp_path / 'identity.db'}",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/api/v1/health").json() == {"status": "ok"}
        assert isinstance(app.state.collection_service.store, TaskStore)
        assert not hasattr(app.state, "recovery_tasks")
```

- [ ] **Step 2: Run the test and verify RED**

Run: `conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/test_health.py::test_app_starts_with_sqlite_and_json_task_store -q`

Expected: FAIL because `create_app` currently constructs `DatabaseTaskStore` and runs PostgreSQL recovery.

- [ ] **Step 3: Restore JSON task wiring and create SQLite account tables**

In `backend/app/main.py`, construct `TaskStore(resolved_settings.data_dir)`, remove JSON-to-PostgreSQL import and lease recovery, and keep the SQLAlchemy engine only for authentication. During lifespan startup call `Base.metadata.create_all(engine)` before accepting requests and dispose the engine on shutdown.

In `backend/app/config.py`, make these values self-contained defaults:

```python
app_env: str = "development"
database_url: str = "sqlite:///./data/identity.db"
app_master_key: SecretStr
session_secret: SecretStr
```

Remove `redis_url` from required settings. Keep explicit `DATABASE_URL` overrides working for deployments that choose another account database.

- [ ] **Step 4: Run focused and authentication tests**

Run: `conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/test_health.py backend/tests/api/test_auth_sessions.py backend/tests/api/test_auth_isolation.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/main.py backend/app/config.py backend/app/db.py backend/tests/conftest.py backend/tests/test_health.py
git commit -m "refactor: run collection without postgres"
```

---

### Task 2: Implement ERP Mapping and Durable Failure Records

**Files:**
- Create: `backend/app/collection/erp_delivery.py`
- Create: `backend/tests/collection/test_erp_delivery.py`
- Modify: `backend/app/config.py`

**Interfaces:**
- Produces: `build_erp_payload(task: dict, lead: dict) -> dict`.
- Produces: `ErpLeadDelivery(data_dir: Path, target_url: str | None, client: httpx.Client | None = None)`.
- Produces: `ErpLeadDelivery.deliver(task: dict, lead: dict) -> DeliveryResult` and `retry(task_id: str | None = None) -> dict[str, int]`.

- [ ] **Step 1: Write failing mapping tests**

```python
def test_build_erp_payload_uses_stable_guid_and_nested_contacts():
    first = build_erp_payload(TASK, LEAD)
    second = build_erp_payload(TASK, {**LEAD, "mobile": "13900000000"})
    assert first["customerAccountGuid"] == second["customerAccountGuid"]
    assert first["customerAccountName"] == LEAD["company_name"]
    assert first["customerCapturedAt"] == "2026-07-25 12:33:23"
    assert first["contactInfo"]["mobilePhone"] == LEAD["mobile"]
    assert "地址：" in first["contactInfo"]["otherContact"]
    assert "来源：" in first["contactInfo"]["otherContact"]
```

Use UUID5 over `city + normalized_company_name`; set `customerSourceChannel` from the lead's discovery channel, `customerType` to `企业客户`, `sourceMethod` to `AI联网采集`, and `cEndCustomerRequirement` to the original request.

- [ ] **Step 2: Run the mapping test and verify RED**

Run: `conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/collection/test_erp_delivery.py::test_build_erp_payload_uses_stable_guid_and_nested_contacts -q`

Expected: FAIL because `erp_delivery.py` does not exist.

- [ ] **Step 3: Implement minimal deterministic mapping**

Implement `build_erp_payload` with `uuid5(NAMESPACE_URL, f"city-lead:{city}:{normalized_name}")`, omit empty optional contact keys, select `contactInfoType` from the available mobile/email/WeChat fields, and format ISO timestamps as `YYYY-MM-DD HH:MM:SS`.

- [ ] **Step 4: Write failing delivery-state tests**

```python
def test_business_error_is_queued_and_does_not_raise(tmp_path):
    client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"errCode": 49302, "errMsg": "IP not whitelisted"})
    ))
    delivery = ErpLeadDelivery(tmp_path, ERP_URL, client=client)
    result = delivery.deliver(TASK, LEAD)
    assert result.status == "failed"
    assert delivery.failure_counts(TASK["id"]) == {"pending": 1}


def test_retry_removes_failure_after_erp_success(tmp_path):
    responses = iter([
        httpx.Response(503),
        httpx.Response(200, json={"errCode": 0, "errMsg": None, "result": {}}),
    ])
    client = httpx.Client(transport=httpx.MockTransport(lambda request: next(responses)))
    delivery = ErpLeadDelivery(tmp_path, ERP_URL, client=client)
    delivery.deliver(TASK, LEAD)
    assert delivery.retry(TASK["id"]) == {"attempted": 1, "succeeded": 1, "failed": 0}
    assert delivery.failure_counts(TASK["id"]) == {"pending": 0}
```

- [ ] **Step 5: Run delivery tests and verify RED**

Run: `conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/collection/test_erp_delivery.py -q`

Expected: mapping test passes; queue and retry tests fail because delivery state is not implemented.

- [ ] **Step 6: Implement atomic delivery state**

Store pending failures at `data/delivery/failures.json` and successful lead fingerprints at `data/delivery/delivered.json`. Write to a sibling `.tmp` file and atomically replace the target under an `RLock`. Treat network errors, non-2xx responses, invalid JSON, and `errCode != 0` as retryable failures. Return a result object instead of raising so collection can continue.

Add settings:

```python
erp_auto_push: bool = False
erp_delivery_url: str = "https://qingflow.com/api/qsource/eab3897c-4748-47b5-9121-96683b36cf0c"
erp_delivery_timeout_seconds: float = 30.0
```

- [ ] **Step 7: Run tests and commit**

Run: `conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/collection/test_erp_delivery.py -q`

Expected: PASS.

```bash
git add backend/app/collection/erp_delivery.py backend/app/config.py backend/tests/collection/test_erp_delivery.py
git commit -m "feat: add durable ERP lead delivery"
```

---

### Task 3: Push Persisted Leads Automatically and Expose Retry Status

**Files:**
- Modify: `backend/app/collection/service.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/api/routes/collection_tasks.py`
- Modify: `backend/tests/collection/test_two_stage_service.py`
- Modify: `backend/tests/api/test_collection_mvp.py`

**Interfaces:**
- Consumes: `ErpLeadDelivery.deliver`, `retry`, and `failure_counts` from Task 2.
- Produces: `CollectionService(..., lead_delivery: ErpLeadDelivery | None = None)`.
- Produces: `GET /api/v1/collection-tasks/{task_id}/delivery-status`.
- Produces: `POST /api/v1/collection-tasks/{task_id}/retry-delivery`.

- [ ] **Step 1: Write failing service integration test**

```python
def test_enriched_lead_is_saved_before_erp_delivery(service, store):
    observed = []

    class RecordingDelivery:
        def deliver(self, task, lead):
            observed.append(store.load(task["id"])["leads"])

    service.lead_delivery = RecordingDelivery()
    service.run_task(TASK_ID)
    assert observed
    assert any(item["mobile"] for item in observed[0])
```

- [ ] **Step 2: Run the test and verify RED**

Run: `conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/collection/test_two_stage_service.py::test_enriched_lead_is_saved_before_erp_delivery -q`

Expected: FAIL because `CollectionService` has no delivery dependency or post-save hook.

- [ ] **Step 3: Add a post-persistence delivery hook**

After an enriched company is saved, call delivery only when company name exists and at least one of mobile/email/WeChat is present. Track the outcome in task activity using `erp_delivery` with `completed`, `failed`, or `skipped`. Catch unexpected delivery exceptions at this boundary, record the error without credentials, and continue the loop.

- [ ] **Step 4: Write failing route tests**

```python
def test_delivery_status_and_retry_routes(collection_client):
    task = create_task(collection_client, ["广州市"], ["软装公司"])
    response = collection_client.get(f"/api/v1/collection-tasks/{task['id']}/delivery-status")
    assert response.json()["pending"] == 0
    retry = collection_client.post(f"/api/v1/collection-tasks/{task['id']}/retry-delivery")
    assert retry.json() == {"attempted": 0, "succeeded": 0, "failed": 0}
```

- [ ] **Step 5: Run route tests and verify RED**

Run: `conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/api/test_collection_mvp.py::test_delivery_status_and_retry_routes -q`

Expected: FAIL with HTTP 404.

- [ ] **Step 6: Wire delivery and add routes**

Construct `ErpLeadDelivery` in `create_app` only when `erp_auto_push` is true; otherwise provide a disabled component whose status remains queryable. Add the two task-scoped routes and verify the task exists before reading or retrying delivery records.

- [ ] **Step 7: Run focused and regression tests**

Run: `conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/collection/test_erp_delivery.py backend/tests/collection/test_two_stage_service.py backend/tests/api/test_collection_mvp.py -q`

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add backend/app/collection/service.py backend/app/main.py backend/app/api/routes/collection_tasks.py backend/tests/collection/test_two_stage_service.py backend/tests/api/test_collection_mvp.py
git commit -m "feat: push collected leads directly to ERP"
```

---

### Task 4: Remove Service Dependencies and Verify the Deliverable

**Files:**
- Modify: `.env.example`
- Modify: `backend/pyproject.toml`
- Delete: `compose.yaml`
- Delete: `backend/app/collection/database_store.py`
- Delete: `backend/app/collection/json_import.py`
- Delete: `backend/app/collection/recovery.py`
- Delete: `backend/tests/collection/test_database_task_store.py`
- Delete: `backend/tests/collection/test_json_import.py`
- Delete: `backend/tests/collection/test_task_recovery.py`

**Interfaces:**
- Consumes: all behavior completed in Tasks 1-3.
- Produces: a local runtime requiring only the `doubao-leads` conda environment and `.env` credentials.

- [ ] **Step 1: Update the environment contract**

Set the example account URL to `DATABASE_URL=sqlite:///./data/identity.db`, remove PostgreSQL/Redis variables, and add:

```dotenv
ERP_AUTO_PUSH=false
ERP_DELIVERY_URL=https://qingflow.com/api/qsource/eab3897c-4748-47b5-9121-96683b36cf0c
ERP_DELIVERY_TIMEOUT_SECONDS=30
```

Do not modify or print the user's real API keys.

- [ ] **Step 2: Remove unused service-only dependencies and code**

Remove `psycopg`, `redis`, and `celery` from `backend/pyproject.toml`. Delete the Compose file and the PostgreSQL collection-store/import/recovery modules and their dedicated tests. Keep Alembic identity migrations for deployments that use them; historical collection migration files may remain but are not part of startup.

- [ ] **Step 3: Run backend verification**

Run: `conda run -n doubao-leads --no-capture-output python -m pytest backend/tests -q`

Expected: all tests PASS with no PostgreSQL, Redis, or Docker process running.

Run: `conda run -n doubao-leads --no-capture-output python -m ruff check backend/app backend/tests`

Expected: PASS.

- [ ] **Step 4: Build and smoke-test the application**

Run: `conda run -n doubao-leads --no-capture-output npm.cmd --prefix frontend run build`

Expected: Vite build succeeds.

Start the backend and verify `GET http://127.0.0.1:8010/api/v1/health` returns `{"status":"ok"}`. Start the frontend and verify `http://127.0.0.1:5173/` loads and can create a natural-language task without PostgreSQL or Redis.

- [ ] **Step 5: Commit**

```bash
git add .env.example backend/pyproject.toml backend/app backend/tests compose.yaml
git commit -m "chore: remove postgres and redis runtime services"
```

