# PostgreSQL Collection Storage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist multi-day collection tasks and tens of thousands of company leads in normalized PostgreSQL tables without Redis.

**Architecture:** SQLAlchemy models and Alembic create normalized task, lead, city-progress, and activity tables. A `DatabaseTaskStore` preserves the current service dictionary contract while writing changed leads incrementally; the FastAPI app uses this store, imports legacy JSON once, and resumes expired running tasks in a single backend instance.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2, Alembic, PostgreSQL 16, psycopg 3, pytest, SQLite for unit tests.

## Global Constraints

- Do not add Redis or Celery.
- Do not store all leads in one JSONB task field.
- Persist each completed company immediately.
- Preserve existing API paths and response field names.
- Preserve source links and verification status; model output is not automatically verified.
- Keep legacy JSON files unchanged after import.
- Never commit API keys or database passwords.

---

### Task 1: Add normalized collection models and migration

**Files:**
- Create: `backend/app/models/collection.py`
- Modify: `backend/app/models/__init__.py`
- Create: `backend/migrations/versions/0002_collection_storage.py`
- Create: `backend/tests/models/test_collection_models.py`

**Interfaces:**
- Produces: `CollectionTask`, `CollectionLead`, `CollectionCityProgress`, and `CollectionActivity` SQLAlchemy models.
- Produces: database tables `collection_tasks`, `collection_leads`, `collection_city_progress`, and `collection_activity`.

- [ ] **Step 1: Write failing model tests**

Create tests that build all metadata in SQLite, insert one task with two leads, and verify the unique constraints:

```python
def test_collection_lead_is_unique_by_task_city_and_normalized_name(session):
    task = CollectionTask(id=uuid4(), name="广州采集", request_text="找软装公司", status="draft")
    session.add(task)
    session.flush()
    session.add(CollectionLead(task_id=task.id, city="广州市", company_name="广州甲公司", normalized_company_name="广州甲公司"))
    session.commit()
    session.add(CollectionLead(task_id=task.id, city="广州市", company_name="广州甲公司", normalized_company_name="广州甲公司"))
    with pytest.raises(IntegrityError):
        session.commit()
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```text
conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/models/test_collection_models.py -q --basetemp=.pytest-pg-model-red
```

Expected: import failure because the collection models do not exist.

- [ ] **Step 3: Implement the models**

Use UUID primary keys, timezone-aware timestamps, JSON for small task configuration arrays, `Text` for descriptions, indexed contact columns, and relationships with `cascade="all, delete-orphan"`. Add:

```python
UniqueConstraint("task_id", "city", "normalized_company_name", name="uq_collection_lead_company")
UniqueConstraint("task_id", "city", name="uq_collection_city_progress")
UniqueConstraint("task_id", "sequence", name="uq_collection_activity_sequence")
```

`CollectionTask` includes status/progress columns plus `heartbeat_at`, `lease_owner`, and `lease_expires_at`.

- [ ] **Step 4: Add Alembic revision `0002_collection_storage`**

Set `down_revision = "0001_identity"`. Create the four tables, foreign keys with `ON DELETE CASCADE`, unique constraints, and indexes for `task_id`, `city`, `mobile`, `email`, `wechat`, `status`, and `lease_expires_at`.

- [ ] **Step 5: Verify models and migration metadata**

Run:

```text
conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/models/test_collection_models.py -q --basetemp=.pytest-pg-model-green
conda run -n doubao-leads --no-capture-output python -m ruff check backend/app/models backend/migrations backend/tests/models
```

Expected: tests pass and Ruff reports no errors.

### Task 2: Implement incremental `DatabaseTaskStore`

**Files:**
- Create: `backend/app/collection/database_store.py`
- Create: `backend/tests/collection/test_database_task_store.py`
- Modify: `backend/app/collection/service.py`

**Interfaces:**
- Consumes: `sessionmaker[Session]`.
- Produces: `DatabaseTaskStore.load(task_id)`, `save(task)`, `save_leads(task_id, leads)`, `list_leads(task_id, city=None, offset=0, limit=None)`, `iter_leads(task_id, batch_size=500)`, and `list_task_ids_by_status(statuses)`.

- [ ] **Step 1: Write failing repository tests**

Cover task round-trip, incremental lead upsert, contact-field update, activity append without duplication, city-progress upsert, pagination, and task-not-found behavior. The key incremental test must call `save_leads` with one changed row and verify another row remains unchanged.

- [ ] **Step 2: Run repository tests and verify RED**

Run:

```text
conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/collection/test_database_task_store.py -q --basetemp=.pytest-pg-store-red
```

Expected: import failure because `DatabaseTaskStore` does not exist.

- [ ] **Step 3: Implement dictionary/model mapping**

Use explicit field maps rather than serializing arbitrary task dictionaries. `load` reconstructs current API-compatible task dictionaries, including `leads`, `city_progress`, and `activity`. Store `_persisted_activity_count` only in memory so `save` inserts `task["activity"][_persisted_activity_count:]` with increasing sequence numbers.

- [ ] **Step 4: Implement incremental lead writes**

For PostgreSQL use SQLAlchemy dialect `insert(...).on_conflict_do_update`; for SQLite tests use its dialect equivalent. Select the dialect from `session.bind.dialect.name`. Update only supplied leads and commit each batch. Never iterate over all task leads inside `save(task)`.

- [ ] **Step 5: Preserve JSON `TaskStore` test compatibility**

Add no-op-compatible `save_leads`, `list_leads`, and `iter_leads` methods to the existing file store so current focused tests continue to run without PostgreSQL.

- [ ] **Step 6: Verify repository behavior**

Run:

```text
conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/collection/test_database_task_store.py backend/tests/collection/test_two_stage_service.py -q --basetemp=.pytest-pg-store-green
conda run -n doubao-leads --no-capture-output python -m ruff check backend/app/collection backend/tests/collection
```

Expected: all tests pass.

### Task 3: Make collection writes incremental and APIs paginated

**Files:**
- Modify: `backend/app/collection/service.py`
- Modify: `backend/app/api/routes/collection_tasks.py`
- Modify: `backend/app/collection/exports.py`
- Modify: `backend/app/collection/delivery.py`
- Modify: `backend/tests/collection/test_two_stage_service.py`
- Modify: `backend/tests/api/test_collection_mvp.py`

**Interfaces:**
- Consumes: `TaskStore.save_leads`, `list_leads`, and `iter_leads`.
- Produces: `GET /api/v1/collection-tasks/{task_id}/leads?city=&offset=0&limit=100` with `items`, `total`, `offset`, and `limit`.

- [ ] **Step 1: Write failing persistence and pagination tests**

Assert that candidate discovery calls `save_leads` immediately, every completed Doubao enrichment calls `save_leads` for that company, and the lead API returns deterministic pages ordered by `saved_at, id`.

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```text
conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/collection/test_two_stage_service.py backend/tests/api/test_collection_mvp.py -q --basetemp=.pytest-pg-service-red
```

Expected: new persistence-call or pagination assertions fail.

- [ ] **Step 3: Persist changed leads at mutation boundaries**

Call `save_leads` after candidate normalization, after each enrichment future completes, after verification batches, and after legacy collection batches. Keep `save(task)` for task summary, progress, city progress, and new activity entries only.

- [ ] **Step 4: Add database pagination and streaming consumers**

Lead API validates `offset >= 0` and `1 <= limit <= 1000`. Exports and delivery iterate all rows in batches of 500 so they do not load tens of thousands of leads at once.

- [ ] **Step 5: Verify API compatibility and incremental writes**

Run:

```text
conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/collection backend/tests/api/test_collection_mvp.py -q --basetemp=.pytest-pg-service-green
```

Expected: all tests pass and existing response fields remain available.

### Task 4: Wire PostgreSQL, import JSON, and resume interrupted tasks

**Files:**
- Modify: `backend/app/main.py`
- Create: `backend/app/collection/json_import.py`
- Create: `backend/app/collection/recovery.py`
- Create: `backend/tests/collection/test_json_import.py`
- Create: `backend/tests/collection/test_task_recovery.py`

**Interfaces:**
- Produces: `import_json_tasks(data_dir, store) -> ImportSummary`.
- Produces: `recover_expired_tasks(service, store, now) -> list[str]`.

- [ ] **Step 1: Write failing import and recovery tests**

Import the same JSON fixture twice and assert one task/lead set exists. Create a `running` task with an expired lease and pending lead, run recovery, and assert it is queued once; an active lease must not be taken over.

- [ ] **Step 2: Run tests and verify RED**

Run:

```text
conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/collection/test_json_import.py backend/tests/collection/test_task_recovery.py -q --basetemp=.pytest-pg-recovery-red
```

Expected: import failures because the modules do not exist.

- [ ] **Step 3: Implement idempotent JSON import**

Scan `data/tasks/*.json`; skip task IDs already present; insert task, city progress, all leads in batches of 500, and activity entries. Do not rename or delete source JSON files.

- [ ] **Step 4: Implement single-instance leases and startup recovery**

Before running a task, acquire a 5-minute lease and refresh heartbeat after each saved lead. At app startup, requeue tasks whose lease expired and schedule `asyncio.to_thread(service.run_task, task_id)` while retaining task references in `app.state.recovery_tasks`. Clear the lease on completion.

- [ ] **Step 5: Wire `DatabaseTaskStore` in `create_app`**

Construct it from `app.state.session_factory`, import JSON during lifespan startup, and recover expired tasks. Tests continue using SQLite through the same database store.

- [ ] **Step 6: Verify import, recovery, and app startup**

Run:

```text
conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/collection/test_json_import.py backend/tests/collection/test_task_recovery.py backend/tests/test_health.py -q --basetemp=.pytest-pg-recovery-green
```

Expected: all tests pass.

### Task 5: Remove Redis and document PostgreSQL runtime configuration

**Files:**
- Modify: `compose.yaml`
- Modify: `backend/pyproject.toml`
- Modify: `backend/app/config.py`
- Modify: `.env.example`
- Modify: `environment.yml`
- Modify: affected tests constructing `Settings`

**Interfaces:**
- Removes: `Settings.redis_url` and unused `redis`/`celery` packages.
- Keeps: `DATABASE_URL` using `postgresql+psycopg://...`.

- [ ] **Step 1: Write a failing configuration test**

Assert `Settings` can be created without `redis_url` and the Compose service list contains only PostgreSQL.

- [ ] **Step 2: Remove Redis/Celery configuration**

Delete the Redis Compose service, `redis` and `celery` project dependencies, conda dependencies, and `redis_url` setting. Update all test settings constructors.

- [ ] **Step 3: Run the complete automated suite**

Run:

```text
conda run -n doubao-leads --no-capture-output python -m pytest backend/tests -q --basetemp=.pytest-pg-release
conda run -n doubao-leads --no-capture-output python -m ruff check backend/app backend/tests backend/migrations
conda run -n doubao-leads --no-capture-output npm.cmd run build
```

Expected: all backend tests, Ruff, and frontend build pass.

### Task 6: PostgreSQL 16 integration and Gitee backup

**Files:**
- Runtime database only.
- Existing legacy files under `data/tasks/` remain unchanged.

**Interfaces:**
- Consumes: Docker Compose PostgreSQL and Alembic migrations.

- [ ] **Step 1: Start PostgreSQL only**

Run `docker compose up -d postgres` with a non-committed local password and wait for readiness.

- [ ] **Step 2: Run Alembic against PostgreSQL**

Run `alembic upgrade head` from `backend` through the `doubao-leads` environment. Expected: revisions `0001_identity` and `0002_collection_storage` apply successfully.

- [ ] **Step 3: Import and validate historical tasks**

Start the backend, confirm existing JSON tasks appear through the unchanged APIs, and compare the Guangzhou task's lead count and contact counts before and after import.

- [ ] **Step 4: Execute a real five-company task**

Create one natural-language task, wait for completion, restart the backend during or after the run, and confirm persisted leads remain queryable and recovery does not duplicate companies.

- [ ] **Step 5: Push verified commits**

Run:

```text
git status --short
git push origin codex/five-city-lead-platform:main
```

Expected: migration and implementation commits reach Gitee `main`; `.env`, database volumes, and secrets remain untracked.

### Task 7: ERP-standard automatic delivery outbox

**Files:**
- Create: `backend/app/models/delivery.py`
- Create: `backend/migrations/versions/0003_lead_delivery_outbox.py`
- Modify: `backend/app/collection/delivery.py`
- Modify: `backend/app/collection/service.py`
- Modify: `backend/app/config.py`
- Modify: `.env.example`
- Create: `backend/tests/collection/test_erp_delivery.py`

**Interfaces:**
- Produces: `build_erp_payload(task, lead) -> dict[str, object]` matching the ERP document.
- Produces: `enqueue_erp_delivery(task, lead, target_url)` with one row per lead and target.
- Produces: `deliver_pending_outbox(batch_size=100)` that accepts success only when HTTP is successful and `errCode == 0`.

- [ ] **Step 1: Write failing ERP mapping and delivery tests**

Assert exact English JSON keys, nested `contactInfo`, stable `customerAccountGuid`, string contact values, `企业客户`, and `AI联网采集`. Mock HTTP 200 with `errCode != 0` and assert failure/retry; mock `errCode = 0` and assert `delivered_at` is stored.

- [ ] **Step 2: Add outbox model and migration**

Create `lead_delivery_outbox` with `lead_id`, `target_url`, `payload`, `status`, `attempts`, `next_attempt_at`, `delivered_at`, `last_error`, and `response_summary`. Add a unique constraint on `lead_id + target_url` and indexes for `status + next_attempt_at`.

- [ ] **Step 3: Implement strict ERP payload mapping**

Generate `customerAccountGuid` with UUID5 over normalized city and company name so repeated runs update the same ERP record. Do not send null optional fields. Put address and `source_url` in `contactInfo.otherContact`.

- [ ] **Step 4: Enqueue after lead persistence and retry failures**

When `ERP_DELIVERY_URL` is configured, enqueue each changed lead after database commit. A single-instance background loop drains pending rows, uses exponential backoff, and treats only `errCode = 0` as success.

- [ ] **Step 5: Verify ERP delivery behavior**

Run:

```text
conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/collection/test_erp_delivery.py -q --basetemp=.pytest-erp-delivery
conda run -n doubao-leads --no-capture-output python -m ruff check backend/app backend/tests backend/migrations
```

Expected: mapping, idempotency, response handling, and retry tests pass.
