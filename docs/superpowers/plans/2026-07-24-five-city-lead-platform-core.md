# Five-City B2B Lead Platform Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a runnable internal-network application that completes the five-city lead workflow with deterministic task orchestration, workspace isolation, evidence-based verification, deduplication, review, city exports, delivery adapters, and audit logs.

**Architecture:** A React/TypeScript administration UI calls a FastAPI API backed by PostgreSQL. Redis and Celery execute deterministic city-keyword-provider work units; provider, export, and delivery boundaries use typed adapters. The first deliverable uses synthetic provider adapters for repeatable tests, and the backend prevents synthetic records from entering official exports or real deliveries.

**Tech Stack:** Python 3.12, FastAPI, Pydantic 2, SQLAlchemy 2, Alembic, PostgreSQL 16, Redis 7, Celery 5, React, TypeScript, Vite, TanStack Query, TanStack Table, Vitest, Testing Library, Playwright, pytest, Docker Compose.

## Global Constraints

- Pilot cities are exactly Wuhan, Changsha, Chengdu, Hefei, and Xi'an, stored and displayed as 武汉、长沙、成都、合肥、西安.
- Default keyword groups are 软装公司、全案软装公司、全案设计公司、整装公司、整案公司; each task stores an immutable plan snapshot.
- Provider identifiers are `doubao` and `deepseek`; all user-facing labels are 豆包 and DeepSeek.
- A formal lead requires at least one verified mobile, email, or WeChat contact; landlines are excluded.
- Models may understand, classify, and extract data but never serve as factual verification evidence.
- Every verified contact must reference a strong source URL, capture time, evidence excerpt or object reference, and content hash.
- Formal exports and real deliveries contain only verified, non-synthetic records.
- Workspace isolation applies to users, tasks, work units, companies, contacts, evidence, credentials, files, deliveries, and audit logs.
- A task completes only after every planned work unit enters a terminal state; terminal failures produce `completed_with_errors`.
- Browser automation never bypasses CAPTCHA, login controls, access controls, or provider terms.
- Credentials never appear in Git, logs, exports, API responses, screenshots, or test fixtures.
- The target-system contract is not available. This plan implements and tests a generic delivery adapter and local receiver; a target-specific adapter requires a separate reviewed plan after its contract is supplied.
- Live Doubao, DeepSeek, enterprise-data, map, and search adapters require a separate reviewed plan after account scopes, API documentation, rate limits, and test credentials are supplied.

---

## Deliverable Boundary

This plan produces a complete, locally runnable and internally deployable five-city workflow using deterministic synthetic adapters. Synthetic records remain visually marked and cannot be formally exported or sent through a non-test delivery adapter. The adapter contracts, credential store, worker orchestration, verification rules, UI, and test harness are the production foundations used by live integrations.

## File Structure

```text
.
├── compose.yaml                         # PostgreSQL, Redis, API, worker, and web services
├── environment.yml                     # Local conda runtime for Python and Node
├── .env.example                        # Non-secret configuration names
├── backend/
│   ├── pyproject.toml                  # Python dependencies and tool configuration
│   ├── alembic.ini
│   ├── migrations/                     # Versioned PostgreSQL schema
│   ├── app/
│   │   ├── main.py                     # FastAPI application factory
│   │   ├── config.py                   # Validated environment settings
│   │   ├── db.py                       # SQLAlchemy engine and transaction dependency
│   │   ├── api/                        # HTTP routers and authorization dependencies
│   │   ├── domain/                     # Pure task, contact, verification, and dedupe rules
│   │   ├── models/                     # SQLAlchemy persistence models
│   │   ├── repositories/               # Workspace-scoped database access
│   │   ├── providers/                  # Model/data/browser adapter contracts and synthetic adapters
│   │   ├── services/                   # Application orchestration, review, export, delivery, and audit
│   │   ├── security/                   # Password, session, credential encryption, and redaction
│   │   └── workers/                    # Celery application and work-unit jobs
│   └── tests/                          # Unit, API, repository, worker, export, and delivery tests
├── frontend/
│   ├── package.json
│   ├── vite.config.ts
│   ├── src/
│   │   ├── api/                        # Typed API client and query hooks
│   │   ├── auth/                       # Session and route guards
│   │   ├── components/                 # Reusable operational controls
│   │   ├── features/                   # Tasks, leads, review, delivery, providers, and audit
│   │   ├── routes/                     # Application route composition
│   │   └── test/                       # MSW and render helpers
│   └── e2e/                            # Playwright user workflows
└── scripts/
    ├── seed_demo.py                    # Clearly synthetic demo workspace and task
    └── verify_release.py               # UTF-8, secret, migration, test, and build checks
```

## Task 1: Bootstrap the Runtime and Health Contract

**Files:**
- Create: `environment.yml`
- Create: `.env.example`
- Create: `compose.yaml`
- Modify: `.gitignore`
- Create: `backend/pyproject.toml`
- Create: `backend/app/__init__.py`
- Create: `backend/app/config.py`
- Create: `backend/app/main.py`
- Create: `backend/tests/test_health.py`

**Interfaces:**
- Consumes: none.
- Produces: `Settings`, `get_settings()`, `create_app(settings: Settings | None = None) -> FastAPI`, and `GET /api/v1/health` returning `{"status": "ok"}`.

- [ ] **Step 1: Create runtime manifests and the failing health test**

```yaml
# environment.yml
name: doubao-leads
channels:
  - conda-forge
dependencies:
  - python=3.12
  - nodejs=22
  - pip
```

```toml
# backend/pyproject.toml
[build-system]
requires = ["setuptools>=75"]
build-backend = "setuptools.build_meta"

[project]
name = "doubao-lead-platform-backend"
version = "0.1.0"
requires-python = ">=3.12,<3.13"
dependencies = [
  "fastapi>=0.115,<1",
  "uvicorn[standard]>=0.34,<1",
  "pydantic-settings>=2.7,<3",
  "sqlalchemy>=2.0,<3",
  "alembic>=1.14,<2",
  "psycopg[binary]>=3.2,<4",
  "redis>=5.2,<6",
  "celery>=5.4,<6",
  "cryptography>=44,<46",
  "pwdlib[argon2]>=0.2,<1",
  "pyjwt>=2.10,<3",
  "httpx>=0.28,<1",
  "openpyxl>=3.1,<4",
  "email-validator>=2.2,<3",
]

[project.optional-dependencies]
dev = [
  "pytest>=8.3,<9",
  "pytest-asyncio>=0.25,<1",
  "pytest-cov>=6,<7",
  "freezegun>=1.5,<2",
  "ruff>=0.9,<1",
]

[tool.setuptools.packages.find]
where = ["."]
include = ["app*"]

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"

[tool.ruff]
line-length = 120
target-version = "py312"
```

```yaml
# compose.yaml, expanded in Task 12
services:
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: ${POSTGRES_DB:-leads}
      POSTGRES_USER: ${POSTGRES_USER:-leads}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}
    ports:
      - "5432:5432"
    volumes:
      - postgres_data:/var/lib/postgresql/data
  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"

volumes:
  postgres_data:
```

```dotenv
# .env.example
APP_ENV=development
POSTGRES_DB=leads
POSTGRES_USER=leads
POSTGRES_PASSWORD=
DATABASE_URL=
REDIS_URL=redis://localhost:6379/0
APP_MASTER_KEY=
SESSION_SECRET=
```

Append these entries to `.gitignore`: `.env`, `*.pem`, `*.key`, `backend/.pytest_cache/`, `backend/.coverage`, `backend/*.egg-info/`, `frontend/node_modules/`, `frontend/dist/`, `frontend/playwright-report/`, and `frontend/test-results/`.

```python
# backend/tests/test_health.py
from secrets import token_urlsafe

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_health_returns_ok() -> None:
    settings = Settings(
        app_env="test",
        database_url="sqlite+pysqlite:///:memory:",
        redis_url="redis://localhost:6379/15",
        app_master_key=Fernet.generate_key().decode("ascii"),
        session_secret=token_urlsafe(32),
    )
    response = TestClient(create_app(settings)).get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
```

- [ ] **Step 2: Create the conda environment and verify the test fails**

Run:

```bash
conda env create -f environment.yml
conda run -n doubao-leads --no-capture-output python -m pip install -e "backend[dev]"
conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/test_health.py -v
```

Expected: FAIL during import because `create_app` does not exist.

- [ ] **Step 3: Implement settings and the application factory**

```python
# backend/app/config.py
from functools import lru_cache
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str
    database_url: str
    redis_url: str
    app_master_key: SecretStr
    session_secret: SecretStr
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

```python
# backend/app/main.py
from fastapi import FastAPI

from app.config import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title="B2B Lead Platform", version="0.1.0")
    app.state.settings = settings or get_settings()

    @app.get("/api/v1/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
```

Run Uvicorn with its factory form, `uvicorn app.main:create_app --factory`, so importing `app.main` never constructs settings before the caller or environment supplies them.

- [ ] **Step 4: Run the focused test and lint checks**

Run:

```bash
conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/test_health.py -v
conda run -n doubao-leads --no-capture-output python -m ruff check backend/app backend/tests
```

Expected: one passing test and no Ruff errors.

- [ ] **Step 5: Commit the bootstrap**

```bash
git add environment.yml .env.example compose.yaml backend
git commit -m "chore: bootstrap lead platform runtime"
```

## Task 2: Add PostgreSQL Models, Workspaces, Authentication, and Credential Security

**Files:**
- Create: `backend/app/db.py`
- Create: `backend/app/models/base.py`
- Create: `backend/app/models/identity.py`
- Create: `backend/app/models/audit.py`
- Create: `backend/app/security/passwords.py`
- Create: `backend/app/security/sessions.py`
- Create: `backend/app/security/credentials.py`
- Create: `backend/app/api/deps.py`
- Create: `backend/app/api/routes/auth.py`
- Create: `backend/app/api/routes/workspaces.py`
- Create: `backend/migrations/versions/0001_identity.py`
- Create: `backend/tests/conftest.py`
- Test: `backend/tests/security/test_credentials.py`
- Test: `backend/tests/api/test_auth_isolation.py`

**Interfaces:**
- Consumes: `Settings` from Task 1.
- Produces: `User`, `Workspace`, `WorkspaceMembership`, `ProviderCredential`, `AuditLog`, `CurrentPrincipal`, `require_roles(*roles)`, `CredentialCipher.encrypt()`, `CredentialCipher.decrypt()`, and `CredentialCipher.mask()`.

- [ ] **Step 1: Write failing encryption and cross-workspace authorization tests**

```python
# backend/tests/security/test_credentials.py
from cryptography.fernet import Fernet

from app.security.credentials import CredentialCipher


def test_credential_cipher_round_trip_does_not_expose_plaintext() -> None:
    cipher = CredentialCipher.from_base64_key(Fernet.generate_key().decode("ascii"))
    encrypted = cipher.encrypt("sk-secret-value")
    assert "sk-secret-value" not in encrypted
    assert cipher.decrypt(encrypted) == "sk-secret-value"
    assert cipher.mask("sk-secret-value") == "sk-********alue"
```

```python
# backend/tests/api/test_auth_isolation.py
def test_member_cannot_read_another_workspace(client, workspace_factory, login_as) -> None:
    first = workspace_factory()
    second = workspace_factory()
    login_as(first.operator)
    response = client.get(f"/api/v1/workspaces/{second.id}")
    assert response.status_code == 404
```

- [ ] **Step 2: Run focused tests to verify missing security boundaries**

Run:

```bash
docker compose up -d postgres redis
conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/security/test_credentials.py backend/tests/api/test_auth_isolation.py -v
```

Expected: FAIL because identity models, fixtures, routes, and credential cipher are absent.

- [ ] **Step 3: Implement identity tables, session cookies, role checks, and encrypted credentials**

Use UUID primary keys. Add unique constraints for user email and workspace membership. Store `ProviderCredential.encrypted_payload`, `provider_name`, `credential_kind`, `last_verified_at`, and `workspace_id`. Implement authenticated cookies as short-lived signed JWTs in `HttpOnly` cookies, and derive `workspace_id` and roles exclusively from the membership table.

```python
# backend/app/security/credentials.py
from cryptography.fernet import Fernet


class CredentialCipher:
    def __init__(self, fernet: Fernet) -> None:
        self._fernet = fernet

    @classmethod
    def from_base64_key(cls, key: str) -> "CredentialCipher":
        return cls(Fernet(key.encode("ascii")))

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        return self._fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")

    def mask(self, plaintext: str) -> str:
        if len(plaintext) <= 8:
            return "*" * len(plaintext)
        return f"{plaintext[:3]}{'*' * 8}{plaintext[-4:]}"
```

All repository methods accept a `workspace_id` argument. `GET /api/v1/workspaces/{workspace_id}` returns workspace metadata only for active members. Cross-workspace lookups return 404, not 403, to avoid exposing object existence.

Create `backend/tests/conftest.py` with transaction-rolled-back PostgreSQL sessions and explicit `client`, `workspace_factory`, `login_as`, and role-specific authenticated-client fixtures. No fixture may use the development database or a committed credential.

- [ ] **Step 4: Apply migrations and run security tests**

Run:

```bash
conda run -n doubao-leads --no-capture-output alembic -c backend/alembic.ini upgrade head
conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/security backend/tests/api/test_auth_isolation.py -v
```

Expected: all security and workspace-isolation tests pass.

- [ ] **Step 5: Commit identity and security foundations**

```bash
git add backend/app backend/migrations backend/tests
git commit -m "feat: add workspace authentication and credential security"
```

## Task 3: Implement Contact Normalization and Evidence Verification

**Files:**
- Create: `backend/app/domain/contacts.py`
- Create: `backend/app/domain/evidence.py`
- Create: `backend/app/domain/verification.py`
- Test: `backend/tests/domain/test_contacts.py`
- Test: `backend/tests/domain/test_verification.py`

**Interfaces:**
- Consumes: none; these modules remain pure domain code.
- Produces: `ContactKind`, `NormalizedContact`, `normalize_contact(kind, value)`, `EvidenceStrength`, `Evidence`, `VerificationDecision`, and `verify_contact(contact, evidence)`.

- [ ] **Step 1: Write failing normalization tests**

```python
# backend/tests/domain/test_contacts.py
import pytest

from app.domain.contacts import ContactKind, InvalidContact, normalize_contact


def test_mobile_normalizes_country_prefix_and_separators() -> None:
    result = normalize_contact(ContactKind.MOBILE, "+86 138-0013-8000")
    assert result.value == "13800138000"


@pytest.mark.parametrize("value", ["027-88886666", "010 8888 6666", "400-800-1234"])
def test_landline_is_rejected(value: str) -> None:
    with pytest.raises(InvalidContact, match="landline"):
        normalize_contact(ContactKind.MOBILE, value)


def test_phone_is_not_inferred_as_wechat() -> None:
    with pytest.raises(InvalidContact, match="explicitly labeled"):
        normalize_contact(ContactKind.WECHAT, "13800138000", explicitly_labeled=False)
```

- [ ] **Step 2: Write failing evidence verification tests and run them**

```python
# backend/tests/domain/test_verification.py
from datetime import UTC, datetime

from app.domain.contacts import ContactKind, normalize_contact
from app.domain.evidence import Evidence, EvidenceStrength
from app.domain.verification import VerificationStatus, verify_contact


def test_strong_source_with_exact_contact_is_verified() -> None:
    contact = normalize_contact(ContactKind.EMAIL, "Sales@Example.com")
    evidence = Evidence(
        source_url="https://example.com/contact",
        source_channel="official_website",
        strength=EvidenceStrength.STRONG,
        excerpt="Contact sales@example.com for projects",
        captured_at=datetime(2026, 7, 24, tzinfo=UTC),
        content_hash="sha256:abc",
        synthetic=False,
    )
    assert verify_contact(contact, [evidence]).status is VerificationStatus.VERIFIED


def test_synthetic_evidence_cannot_verify() -> None:
    contact = normalize_contact(ContactKind.EMAIL, "sales@example.com")
    evidence = Evidence.synthetic("sales@example.com")
    assert verify_contact(contact, [evidence]).status is VerificationStatus.PENDING_REVIEW
```

Run:

```bash
conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/domain/test_contacts.py backend/tests/domain/test_verification.py -v
```

Expected: FAIL because the domain modules are absent.

- [ ] **Step 3: Implement explicit normalization and verification decisions**

Implement Chinese mobile validation with `^1[3-9][0-9]{9}$`, email normalization with `email-validator`, and WeChat validation only when `explicitly_labeled=True`. Verification returns `verified` only when a non-synthetic strong evidence item contains the normalized contact exactly after type-appropriate normalization. Weak, missing, conflicting, unreachable, or synthetic evidence returns `pending_review` or `rejected` with a stable reason code.

- [ ] **Step 4: Run the complete domain test set**

Run:

```bash
conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/domain -v
```

Expected: all contact and evidence tests pass.

- [ ] **Step 5: Commit the evidence domain**

```bash
git add backend/app/domain backend/tests/domain
git commit -m "feat: add contact normalization and evidence verification"
```

## Task 4: Implement Deterministic Task Planning and State Aggregation

**Files:**
- Create: `backend/app/domain/tasks.py`
- Create: `backend/app/services/task_planner.py`
- Create: `backend/app/models/tasks.py`
- Create: `backend/app/repositories/tasks.py`
- Create: `backend/migrations/versions/0002_tasks.py`
- Test: `backend/tests/domain/test_task_planner.py`
- Test: `backend/tests/domain/test_task_states.py`

**Interfaces:**
- Consumes: workspace identity from Task 2.
- Produces: `TaskPlanRequest`, `WorkUnitSpec`, `build_plan(request) -> tuple[WorkUnitSpec, ...]`, `TaskStatus`, `WorkUnitStatus`, and `aggregate_task_status(statuses) -> TaskStatus`.

- [ ] **Step 1: Write the failing matrix and completion tests**

```python
# backend/tests/domain/test_task_planner.py
from app.services.task_planner import TaskPlanRequest, build_plan


def test_plan_is_complete_stable_cartesian_product() -> None:
    request = TaskPlanRequest(
        cities=("武汉", "长沙"),
        keywords=("软装公司", "整装公司"),
        models=("doubao", "deepseek"),
        sources=("synthetic_directory",),
        result_limit=20,
    )
    first = build_plan(request)
    second = build_plan(request)
    assert len(first) == 8
    assert [unit.idempotency_key for unit in first] == [unit.idempotency_key for unit in second]
    assert len(set(unit.idempotency_key for unit in first)) == 8
```

```python
# backend/tests/domain/test_task_states.py
from app.domain.tasks import TaskStatus, WorkUnitStatus, aggregate_task_status


def test_terminal_failure_produces_completed_with_errors() -> None:
    statuses = [WorkUnitStatus.SUCCEEDED, WorkUnitStatus.NO_RESULT, WorkUnitStatus.FAILED]
    assert aggregate_task_status(statuses) is TaskStatus.COMPLETED_WITH_ERRORS


def test_non_terminal_unit_keeps_task_running() -> None:
    statuses = [WorkUnitStatus.SUCCEEDED, WorkUnitStatus.RETRY_WAIT]
    assert aggregate_task_status(statuses) is TaskStatus.RUNNING
```

- [ ] **Step 2: Run focused tests and confirm failure**

Run:

```bash
conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/domain/test_task_planner.py backend/tests/domain/test_task_states.py -v
```

Expected: FAIL because planning and state aggregation are absent.

- [ ] **Step 3: Implement immutable plan snapshots and status transitions**

Generate each work-unit idempotency key as SHA-256 over a canonical JSON object containing task plan version, city, keyword, model, source, and result limit. Persist the original natural-language request, structured request, frozen plan JSON, plan version, city summary rows, and work units. Enforce valid transitions with a pure `transition(current, event)` function; reject transitions from terminal states.

- [ ] **Step 4: Apply the migration and run planner tests**

Run:

```bash
conda run -n doubao-leads --no-capture-output alembic -c backend/alembic.ini upgrade head
conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/domain/test_task_planner.py backend/tests/domain/test_task_states.py -v
```

Expected: all planner and task-state tests pass.

- [ ] **Step 5: Commit deterministic planning**

```bash
git add backend/app backend/migrations backend/tests
git commit -m "feat: add deterministic lead task planning"
```

## Task 5: Add Provider Contracts, Synthetic Adapters, and Worker Execution

**Files:**
- Create: `backend/app/providers/base.py`
- Create: `backend/app/providers/registry.py`
- Create: `backend/app/providers/synthetic.py`
- Create: `backend/app/services/work_unit_runner.py`
- Create: `backend/app/workers/celery_app.py`
- Create: `backend/app/workers/jobs.py`
- Create: `backend/app/models/provider_runs.py`
- Create: `backend/migrations/versions/0003_provider_runs.py`
- Test: `backend/tests/providers/test_provider_contract.py`
- Test: `backend/tests/workers/test_work_unit_runner.py`

**Interfaces:**
- Consumes: `WorkUnitSpec`, task repositories, and workspace credentials.
- Produces: `ProviderKind`, `DiscoveryRequest`, `CandidateRecord`, `ProviderResult`, `ProviderError`, `ProviderAdapter.run(request)`, `ProviderRegistry.get(name)`, and `run_work_unit(work_unit_id)`.

- [ ] **Step 1: Write adapter contract and retry-classification tests**

```python
# backend/tests/providers/test_provider_contract.py
import pytest

from app.providers.base import DiscoveryRequest
from app.providers.synthetic import SyntheticDirectoryAdapter


@pytest.mark.asyncio
async def test_synthetic_adapter_marks_every_candidate() -> None:
    adapter = SyntheticDirectoryAdapter(seed=7)
    result = await adapter.run(DiscoveryRequest(city="武汉", keyword="软装公司", limit=2))
    assert len(result.candidates) == 2
    assert all(candidate.synthetic for candidate in result.candidates)
    assert all(evidence.synthetic for candidate in result.candidates for evidence in candidate.evidence)
```

```python
# backend/tests/workers/test_work_unit_runner.py
def test_login_error_moves_only_unit_to_needs_attention(runner, unit_factory, provider_registry) -> None:
    unit = unit_factory(provider="expired_account")
    provider_registry.fail_with_login_required("expired_account")
    runner.run(unit.id)
    assert unit_factory.reload(unit.id).status == "needs_attention"
    assert unit_factory.sibling(unit.task_id).status == "queued"
```

- [ ] **Step 2: Run provider and runner tests to verify failure**

Run:

```bash
conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/providers backend/tests/workers/test_work_unit_runner.py -v
```

Expected: FAIL because adapter contracts and runner do not exist.

- [ ] **Step 3: Implement typed adapters and explicit error classes**

Define stable provider errors: `transient`, `rate_limited`, `login_required`, `captcha_required`, `invalid_response`, and `permanent`. The runner retries only transient and rate-limited errors, using the work unit's configured maximum attempts. Login and CAPTCHA errors become `needs_attention`. Invalid response and permanent errors become `failed` with a redacted error record. Persist provider run timing, result counts, usage units, and raw-object reference.

The synthetic adapter generates deterministic companies, addresses, service descriptions, contacts, and synthetic evidence from its seed. Its provider name starts with `synthetic_`, and `CandidateRecord.synthetic` is immutable.

- [ ] **Step 4: Run worker tests including idempotent redelivery**

Run:

```bash
conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/providers backend/tests/workers -v
```

Expected: all adapter, retry, attention, and idempotency tests pass.

- [ ] **Step 5: Commit provider and worker foundations**

```bash
git add backend/app backend/migrations backend/tests
git commit -m "feat: add provider contracts and work unit execution"
```

## Task 6: Persist Evidence, Leads, Reviews, and Deduplicate Contacts

**Files:**
- Create: `backend/app/models/leads.py`
- Create: `backend/app/repositories/leads.py`
- Create: `backend/app/domain/deduplication.py`
- Create: `backend/app/services/lead_ingestion.py`
- Create: `backend/app/services/lead_review.py`
- Create: `backend/migrations/versions/0004_leads.py`
- Test: `backend/tests/domain/test_deduplication.py`
- Test: `backend/tests/services/test_lead_ingestion.py`
- Test: `backend/tests/services/test_lead_review.py`

**Interfaces:**
- Consumes: candidate records from Task 5 and normalization/verification from Task 3.
- Produces: `Company`, `CompanyProfile`, `Contact`, `LeadEvidence`, `LeadVerification`, `TaskLead`, `ingest_candidates()`, `merge_company_candidates()`, and `review_lead()`.

- [ ] **Step 1: Write failing strong-key dedupe and conflict tests**

```python
# backend/tests/domain/test_deduplication.py
from app.domain.deduplication import CompanyCandidate, merge_company_candidates


def test_same_mobile_merges_sources_for_same_company() -> None:
    first = CompanyCandidate(city="武汉", name="示例软装有限公司", mobiles=("13800138000",), source="doubao")
    second = CompanyCandidate(city="武汉", name="示例软装有限公司", mobiles=("13800138000",), source="official_site")
    merged = merge_company_candidates([first, second])
    assert len(merged.companies) == 1
    assert merged.companies[0].sources == ("doubao", "official_site")


def test_same_mobile_on_different_companies_creates_conflict() -> None:
    first = CompanyCandidate(city="武汉", name="甲公司", mobiles=("13800138000",), source="directory")
    second = CompanyCandidate(city="武汉", name="乙公司", mobiles=("13800138000",), source="map")
    merged = merge_company_candidates([first, second])
    assert merged.conflicts[0].reason == "contact_company_conflict"
```

- [ ] **Step 2: Write failing ingestion rule for synthetic data**

```python
# backend/tests/services/test_lead_ingestion.py
def test_synthetic_candidate_never_becomes_verified(ingestion_service, synthetic_candidate) -> None:
    lead = ingestion_service.ingest(synthetic_candidate)
    assert lead.status == "pending_review"
    assert lead.synthetic is True
```

Run:

```bash
conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/domain/test_deduplication.py backend/tests/services/test_lead_ingestion.py -v
```

Expected: FAIL because lead persistence and merge rules are absent.

- [ ] **Step 3: Implement workspace-scoped uniqueness and append-only evidence**

Create unique constraints on `(workspace_id, contact_kind, normalized_value)`. Use `(workspace_id, city, normalized_company_name)` as a conservative company helper key. Never fuzzy-merge different strong contacts. Persist conflicting company ownership in a review queue. Evidence rows are append-only; profile and address changes create new versions rather than replacing source history.

`review_lead()` records reviewer, timestamp, decision, and reason. A human approval may change `pending_review` to `verified` only for non-synthetic records that still contain a valid non-landline contact and at least one retained strong source containing that contact. Weak-source records remain pending until strong evidence is added. Synthetic records remain non-exportable even after review.

- [ ] **Step 4: Apply migration and run service tests**

Run:

```bash
conda run -n doubao-leads --no-capture-output alembic -c backend/alembic.ini upgrade head
conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/domain/test_deduplication.py backend/tests/services/test_lead_ingestion.py backend/tests/services/test_lead_review.py -v
```

Expected: all dedupe, ingestion, conflict, and review tests pass.

- [ ] **Step 5: Commit lead governance**

```bash
git add backend/app backend/migrations backend/tests
git commit -m "feat: add evidence-based lead governance"
```

## Task 7: Expose Task, Lead, Review, Provider, and Audit APIs

**Files:**
- Create: `backend/app/api/router.py`
- Create: `backend/app/api/routes/tasks.py`
- Create: `backend/app/api/routes/leads.py`
- Create: `backend/app/api/routes/providers.py`
- Create: `backend/app/api/routes/audit.py`
- Create: `backend/app/schemas/tasks.py`
- Create: `backend/app/schemas/leads.py`
- Create: `backend/app/services/audit.py`
- Modify: `backend/app/models/audit.py`
- Create: `backend/migrations/versions/0005_audit_retention.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/api/test_task_lifecycle.py`
- Test: `backend/tests/api/test_lead_review.py`
- Test: `backend/tests/api/test_audit.py`

**Interfaces:**
- Consumes: services and repositories from Tasks 2 through 6.
- Produces: versioned `/api/v1` endpoints for tasks, work units, leads, reviews, providers, and audit logs.

- [ ] **Step 1: Write failing task lifecycle API test**

```python
# backend/tests/api/test_task_lifecycle.py
def test_operator_plans_and_starts_five_city_task(authenticated_operator_client) -> None:
    response = authenticated_operator_client.post(
        "/api/v1/tasks",
        json={
            "name": "家具 B 端五城试点",
            "request_text": "寻找全案软装、设计和整装公司",
            "cities": ["武汉", "长沙", "成都", "合肥", "西安"],
            "keywords": ["软装公司", "全案软装公司", "全案设计公司", "整装公司", "整案公司"],
            "models": ["doubao", "deepseek"],
            "sources": ["synthetic_directory"],
            "result_limit": 20,
        },
    )
    assert response.status_code == 201
    task_id = response.json()["id"]
    plan = authenticated_operator_client.post(f"/api/v1/tasks/{task_id}/plan")
    assert plan.status_code == 200
    assert plan.json()["city_count"] == 5
    assert authenticated_operator_client.post(f"/api/v1/tasks/{task_id}/start").status_code == 202
```

- [ ] **Step 2: Run API tests to verify routes are absent**

Run:

```bash
conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/api/test_task_lifecycle.py backend/tests/api/test_lead_review.py backend/tests/api/test_audit.py -v
```

Expected: FAIL with 404 responses for the new routes.

- [ ] **Step 3: Implement routes, role gates, pagination, and audit records**

Implement create, plan, start, pause, resume, cancel, task detail, and work-unit list endpoints. Implement lead filters by city, contact type, discovery channel, verification status, and task. Implement evidence detail and review endpoints. Provider endpoints expose only masked credential metadata and connection status.

Every mutating endpoint writes `AuditLog` with principal, workspace, request ID, action, target type, target ID, redacted change summary, timestamp, and source address. Audit queries require administrator or read-only-auditor role and remain workspace scoped.

Set `AuditLog.retention_expires_at` to 180 days after creation by default. Add `AuditService.prune_expired(now)` and test that it deletes only expired logs in the selected workspace while retaining legal-hold records and current logs.

- [ ] **Step 4: Run API and OpenAPI contract tests**

Run:

```bash
conda run -n doubao-leads --no-capture-output alembic -c backend/alembic.ini upgrade head
conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/api -v
```

Expected: all API, role, isolation, pagination, and audit tests pass.

- [ ] **Step 5: Commit the versioned API**

```bash
git add backend/app backend/migrations backend/tests/api
git commit -m "feat: expose task lead and audit APIs"
```

## Task 8: Implement City Exports and Idempotent Delivery Adapters

**Files:**
- Create: `backend/app/services/exports.py`
- Create: `backend/app/storage/base.py`
- Create: `backend/app/storage/local.py`
- Create: `backend/app/delivery/base.py`
- Create: `backend/app/delivery/http.py`
- Create: `backend/app/delivery/local_test.py`
- Create: `backend/app/services/deliveries.py`
- Create: `backend/app/models/deliveries.py`
- Create: `backend/app/api/routes/exports.py`
- Create: `backend/app/api/routes/deliveries.py`
- Create: `backend/migrations/versions/0006_deliveries.py`
- Test: `backend/tests/services/test_exports.py`
- Test: `backend/tests/services/test_deliveries.py`
- Test: `backend/tests/storage/test_local_storage.py`

**Interfaces:**
- Consumes: verified lead queries, audit service, and workspace storage boundary.
- Produces: `ObjectStorage.put()`, `ObjectStorage.open()`, `ObjectStorage.delete()`, `ExportService.create_city_export()`, `ExportService.create_task_zip()`, `DeliveryAdapter.send_batch()`, `DeliveryService.deliver()`, and export/delivery API routes.

- [ ] **Step 1: Write failing export safety tests**

```python
# backend/tests/services/test_exports.py
def test_formal_export_contains_only_verified_real_leads(export_service, lead_factory) -> None:
    verified = lead_factory(status="verified", synthetic=False, mobile="13800138000")
    lead_factory(status="pending_review", synthetic=False, email="pending@example.com")
    lead_factory(status="verified", synthetic=True, email="synthetic@example.com")
    workbook = export_service.create_city_export(verified.task_id, "武汉", formal=True)
    rows = workbook.rows("已核验")
    assert [row["手机号码"] for row in rows] == ["13800138000"]
    assert all("synthetic@example.com" not in row.values() for row in rows)
```

- [ ] **Step 2: Write failing delivery idempotency test and run both tests**

```python
# backend/tests/services/test_deliveries.py
def test_retry_does_not_resend_successful_lead(delivery_service, receiver, verified_leads) -> None:
    first = delivery_service.deliver(verified_leads, receiver)
    second = delivery_service.deliver(verified_leads, receiver)
    assert first.sent_count == len(verified_leads)
    assert second.sent_count == 0
    assert second.already_delivered_count == len(verified_leads)
```

```python
# backend/tests/storage/test_local_storage.py
import pytest


def test_workspace_storage_rejects_path_traversal(local_storage, workspace_id) -> None:
    with pytest.raises(ValueError, match="invalid object key"):
        local_storage.put(workspace_id, "../outside.csv", b"forbidden")
```

Run:

```bash
conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/services/test_exports.py backend/tests/services/test_deliveries.py backend/tests/storage/test_local_storage.py -v
```

Expected: FAIL because export and delivery services are absent.

- [ ] **Step 3: Implement official fields, ZIP packaging, and partial delivery retries**

Create CSV and XLSX columns in this order: 城市、公司名称、手机号码、邮箱号码、微信号码、公司地址、服务定位介绍、核验状态、发现渠道、核验来源渠道、来源 URL、采集时间、核验时间、保存日期、任务标识. Aggregate unique contacts by company. Use filenames containing city, sanitized task name, and UTC timestamp.

Map discovery provider IDs to user-facing values when exporting, so `doubao` becomes 豆包 and `deepseek` becomes DeepSeek. Keep discovery channels separate from evidence channels even when they happen to be the same provider.

Store delivery attempts per lead with a unique `(workspace_id, adapter_name, lead_id, lead_version)` idempotency key. Retry failed items only. Record response status and redacted response excerpt. Reject any non-test delivery containing a synthetic or non-verified lead.

The local object-storage adapter resolves every path under a configured workspace root, rejects traversal and absolute paths, and stores content hashes beside export metadata. Download routes authorize the workspace before resolving an object key.

- [ ] **Step 4: Apply migration and run export/delivery tests**

Run:

```bash
conda run -n doubao-leads --no-capture-output alembic -c backend/alembic.ini upgrade head
conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/services/test_exports.py backend/tests/services/test_deliveries.py backend/tests/storage/test_local_storage.py -v
```

Expected: export content, file naming, ZIP, idempotency, partial retry, and safety tests pass.

- [ ] **Step 5: Commit formal delivery behavior**

```bash
git add backend/app backend/migrations backend/tests
git commit -m "feat: add verified exports and idempotent delivery"
```

## Task 9: Build the React Shell, Authentication, and Typed API Client

**Files:**
- Create: `frontend/package.json`
- Create: `frontend/tsconfig.json`
- Create: `frontend/vite.config.ts`
- Create: `frontend/eslint.config.js`
- Create: `frontend/index.html`
- Create: `frontend/src/main.tsx`
- Create: `frontend/src/routes/AppRouter.tsx`
- Create: `frontend/src/auth/AuthProvider.tsx`
- Create: `frontend/src/auth/RequireSession.tsx`
- Create: `frontend/src/api/client.ts`
- Create: `frontend/src/components/AppShell.tsx`
- Create: `frontend/src/test/render.tsx`
- Create: `frontend/src/test/server.ts`
- Create: `frontend/src/test/setup.ts`
- Test: `frontend/src/auth/RequireSession.test.tsx`

**Interfaces:**
- Consumes: auth and OpenAPI endpoints from Task 7.
- Produces: `apiClient`, `AuthProvider`, `useAuth()`, `RequireSession`, and the application navigation shell.

- [ ] **Step 1: Create frontend manifests and the failing route-guard test**

```json
{
  "name": "doubao-lead-platform-frontend",
  "private": true,
  "version": "0.1.0",
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "tsc -b && vite build",
    "typecheck": "tsc -b --pretty false",
    "lint": "eslint . --max-warnings 0",
    "test": "vitest",
    "e2e": "playwright test",
    "generate:api": "openapi-typescript http://localhost:8000/openapi.json -o src/api/schema.d.ts"
  },
  "dependencies": {
    "@hookform/resolvers": "^5.0.0",
    "@tanstack/react-query": "^5.0.0",
    "@tanstack/react-table": "^8.0.0",
    "lucide-react": "^0.500.0",
    "react": "^19.0.0",
    "react-dom": "^19.0.0",
    "react-hook-form": "^7.0.0",
    "react-router-dom": "^7.0.0",
    "zod": "^4.0.0"
  },
  "devDependencies": {
    "@eslint/js": "^9.0.0",
    "@playwright/test": "^1.50.0",
    "@testing-library/jest-dom": "^6.0.0",
    "@testing-library/react": "^16.0.0",
    "@testing-library/user-event": "^14.0.0",
    "@types/node": "^22.0.0",
    "@types/react": "^19.0.0",
    "@types/react-dom": "^19.0.0",
    "@vitejs/plugin-react": "^4.0.0",
    "eslint": "^9.0.0",
    "eslint-plugin-react-hooks": "^5.0.0",
    "eslint-plugin-react-refresh": "^0.4.0",
    "jsdom": "^26.0.0",
    "msw": "^2.0.0",
    "openapi-typescript": "^7.0.0",
    "typescript": "^5.7.0",
    "typescript-eslint": "^8.0.0",
    "vite": "^6.0.0",
    "vitest": "^3.0.0"
  }
}
```

```tsx
// frontend/src/auth/RequireSession.test.tsx
import { screen } from "@testing-library/react";
import { Route, Routes } from "react-router-dom";
import { renderApp } from "../test/render";
import { RequireSession } from "./RequireSession";

test("redirects an anonymous user to login", async () => {
  renderApp(
    <Routes>
      <Route path="/login" element={<div>登录</div>} />
      <Route element={<RequireSession />}>
        <Route path="/tasks" element={<div>任务中心</div>} />
      </Route>
    </Routes>,
    { route: "/tasks", authenticated: false },
  );
  expect(await screen.findByText("登录")).toBeInTheDocument();
});
```

Use React, React Router, TanStack Query, TanStack Table, React Hook Form, Zod, Lucide React, Vite, Vitest, Testing Library, MSW, and Playwright. Generate TypeScript API types from `/openapi.json` using `openapi-typescript`.

- [ ] **Step 2: Install dependencies and verify the test fails**

Run:

```bash
conda run -n doubao-leads --no-capture-output npm.cmd --prefix frontend install
conda run -n doubao-leads --no-capture-output npm.cmd --prefix frontend test -- --run src/auth/RequireSession.test.tsx
```

Expected: FAIL because the auth provider and guard do not exist.

- [ ] **Step 3: Implement same-origin credentials and operational navigation**

The API client always sends `credentials: "include"`, converts non-2xx responses into typed errors, and never stores tokens in local storage. The shell provides routes for 任务中心、线索库、待复核、导出与 API、渠道账号、用户与审计. Render navigation as a sidebar on desktop and a horizontally scrollable tab row on narrow screens.

- [ ] **Step 4: Run frontend tests, type checking, and linting**

Run:

```bash
conda run -n doubao-leads --no-capture-output npm.cmd --prefix frontend test -- --run
conda run -n doubao-leads --no-capture-output npm.cmd --prefix frontend run typecheck
conda run -n doubao-leads --no-capture-output npm.cmd --prefix frontend run lint
```

Expected: all tests pass with zero TypeScript and lint errors.

- [ ] **Step 5: Commit the authenticated UI shell**

```bash
git add frontend
git commit -m "feat: add authenticated operations shell"
```

## Task 10: Build Task Creation, Planning, and Progress Views

**Files:**
- Create: `frontend/src/features/tasks/TaskListPage.tsx`
- Create: `frontend/src/features/tasks/CreateTaskPage.tsx`
- Create: `frontend/src/features/tasks/TaskDetailPage.tsx`
- Create: `frontend/src/features/tasks/TaskPlanSummary.tsx`
- Create: `frontend/src/features/tasks/CityProgressTable.tsx`
- Create: `frontend/src/features/tasks/taskQueries.ts`
- Test: `frontend/src/features/tasks/CreateTaskPage.test.tsx`
- Test: `frontend/src/features/tasks/TaskDetailPage.test.tsx`

**Interfaces:**
- Consumes: typed API client from Task 9 and task endpoints from Task 7.
- Produces: task form, frozen-plan confirmation, start/pause/resume/cancel controls, city progress, and work-unit exception views.

- [ ] **Step 1: Write the failing five-city creation test**

```tsx
// frontend/src/features/tasks/CreateTaskPage.test.tsx
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderApp } from "../../test/render";
import { CreateTaskPage } from "./CreateTaskPage";

test("creates and plans the five-city default workflow", async () => {
  renderApp(<CreateTaskPage />, { authenticated: true });
  await userEvent.type(screen.getByLabelText("任务名称"), "家具 B 端五城试点");
  await userEvent.type(screen.getByLabelText("目标描述"), "寻找全案软装、设计和整装公司");
  await userEvent.click(screen.getByRole("button", { name: "生成执行计划" }));
  expect(await screen.findByText("5 个城市")).toBeInTheDocument();
  expect(screen.getByText("50 个工作单元")).toBeInTheDocument();
});
```

- [ ] **Step 2: Run task UI tests to verify failure**

Run:

```bash
conda run -n doubao-leads --no-capture-output npm.cmd --prefix frontend test -- --run src/features/tasks
```

Expected: FAIL because task pages and query hooks are absent.

- [ ] **Step 3: Implement the task workflow and stable dimensions**

Preselect the five pilot cities and five default keywords. Allow model single-select or multi-select and source multi-select. Before start, show exact city, keyword, model, source, and total work-unit counts. The detail page shows overall progress, verified count, review count, exception count, and a stable city table with work units, progress, counts, and latest activity. A `needs_attention` row exposes a clear account-resolution action; other cities remain visibly active.

- [ ] **Step 4: Run task UI tests and production build**

Run:

```bash
conda run -n doubao-leads --no-capture-output npm.cmd --prefix frontend test -- --run src/features/tasks
conda run -n doubao-leads --no-capture-output npm.cmd --prefix frontend run build
```

Expected: task component tests pass and Vite emits a production build without warnings treated as errors.

- [ ] **Step 5: Commit the task operations UI**

```bash
git add frontend/src/features/tasks frontend/src/routes
git commit -m "feat: add five-city task operations"
```

## Task 11: Build Lead Review, Export, Delivery, Provider, and Audit Views

**Files:**
- Create: `frontend/src/features/leads/LeadLibraryPage.tsx`
- Create: `frontend/src/features/review/ReviewQueuePage.tsx`
- Create: `frontend/src/features/review/EvidencePanel.tsx`
- Create: `frontend/src/features/delivery/DeliveryPage.tsx`
- Create: `frontend/src/features/providers/ProviderAccountsPage.tsx`
- Create: `frontend/src/features/audit/AuditLogPage.tsx`
- Test: `frontend/src/features/review/ReviewQueuePage.test.tsx`
- Test: `frontend/src/features/delivery/DeliveryPage.test.tsx`
- Test: `frontend/src/features/providers/ProviderAccountsPage.test.tsx`

**Interfaces:**
- Consumes: lead, review, export, delivery, provider, and audit endpoints.
- Produces: the remaining daily-operation and administration screens.

- [ ] **Step 1: Write failing evidence and export-safety UI tests**

```tsx
// frontend/src/features/review/ReviewQueuePage.test.tsx
import { screen } from "@testing-library/react";

import { renderApp } from "../../test/render";
import { ReviewQueuePage } from "./ReviewQueuePage";

test("shows source evidence beside a pending contact", async () => {
  renderApp(<ReviewQueuePage />, { authenticated: true, role: "reviewer" });
  expect(await screen.findByText("待人工复核")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "打开来源" })).toHaveAttribute("href", "https://example.com/contact");
  expect(screen.getByText("来源强度：弱来源")).toBeInTheDocument();
});
```

```tsx
// frontend/src/features/delivery/DeliveryPage.test.tsx
import { screen } from "@testing-library/react";

import { renderApp } from "../../test/render";
import { DeliveryPage } from "./DeliveryPage";

test("formal export explicitly excludes pending records", async () => {
  renderApp(<DeliveryPage />, { authenticated: true, role: "operator" });
  expect(await screen.findByText("正式导出仅包含已核验数据")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "导出已核验数据" })).toBeEnabled();
});
```

- [ ] **Step 2: Run feature tests to verify failure**

Run:

```bash
conda run -n doubao-leads --no-capture-output npm.cmd --prefix frontend test -- --run src/features/review src/features/delivery src/features/providers
```

Expected: FAIL because the feature pages are absent.

- [ ] **Step 3: Implement dense operational tables and explicit safety states**

The lead library filters by city, task, contact type, discovery channel, evidence channel, and verification status. The review screen keeps company fields, normalized contact, evidence excerpt, source URL, capture time, and decision controls visible together. Provider accounts show masked identifiers, credential kind, connection state, and last verification time only. Audit rows show actor, time, task or object, action, and result; sensitive payloads are never rendered.

- [ ] **Step 4: Run frontend tests and accessibility checks**

Run:

```bash
conda run -n doubao-leads --no-capture-output npm.cmd --prefix frontend test -- --run
conda run -n doubao-leads --no-capture-output npm.cmd --prefix frontend run typecheck
conda run -n doubao-leads --no-capture-output npm.cmd --prefix frontend run build
```

Expected: all component tests, type checking, and production build pass.

- [ ] **Step 5: Commit lead governance and administration UI**

```bash
git add frontend/src/features frontend/src/routes
git commit -m "feat: add lead review delivery and audit UI"
```

## Task 12: Complete Demo Seeding, End-to-End Tests, Deployment, and Release Verification

**Files:**
- Create: `scripts/seed_demo.py`
- Create: `scripts/verify_release.py`
- Create: `frontend/e2e/five-city-workflow.spec.ts`
- Create: `frontend/playwright.config.ts`
- Create: `backend/Dockerfile`
- Create: `frontend/Dockerfile`
- Create: `frontend/nginx.conf`
- Modify: `compose.yaml`
- Modify: `.env.example`
- Create: `README.md`
- Test: `backend/tests/integration/test_full_workflow.py`

**Interfaces:**
- Consumes: all prior tasks.
- Produces: a reproducible demo workspace, full integration test, browser acceptance workflow, container deployment, and one release verification command.

- [ ] **Step 1: Write the failing full-workflow integration test**

```python
# backend/tests/integration/test_full_workflow.py
def test_five_city_workflow_reaches_terminal_state_without_formal_synthetic_export(app_services) -> None:
    task = app_services.demo.create_five_city_task()
    app_services.tasks.start(task.id)
    app_services.workers.drain()
    finished = app_services.tasks.get(task.id)
    assert finished.status == "completed"
    assert {city.name for city in finished.cities} == {"武汉", "长沙", "成都", "合肥", "西安"}
    assert all(unit.status in {"succeeded", "no_result"} for unit in finished.work_units)
    assert finished.pending_review_count > 0
    formal = app_services.exports.create_task_zip(task.id, formal=True)
    assert formal.exported_lead_count == 0
    assert formal.excluded_synthetic_count == finished.pending_review_count
```

- [ ] **Step 2: Write the failing Playwright workflow**

```ts
// frontend/e2e/five-city-workflow.spec.ts
import { expect, test } from "@playwright/test";

test("operator starts and inspects the five-city demo", async ({ page }) => {
  const demoPassword = process.env.E2E_DEMO_PASSWORD;
  if (!demoPassword) throw new Error("E2E_DEMO_PASSWORD is required");
  await page.goto("/login");
  await page.getByLabel("邮箱").fill("operator@example.test");
  await page.getByLabel("密码").fill(demoPassword);
  await page.getByRole("button", { name: "登录" }).click();
  await page.getByRole("button", { name: "新建任务" }).click();
  await page.getByLabel("任务名称").fill("家具 B 端五城试点");
  await page.getByLabel("目标描述").fill("寻找全案软装、设计和整装公司");
  await page.getByRole("button", { name: "生成执行计划" }).click();
  await expect(page.getByText("5 个城市")).toBeVisible();
  await page.getByRole("button", { name: "启动任务" }).click();
  await expect(page.getByRole("cell", { name: "武汉" })).toBeVisible();
  await expect(page.getByText("模拟数据禁止正式交付")).toBeVisible();
});
```

- [ ] **Step 3: Run integration and browser tests to verify missing release wiring**

Run:

```bash
docker compose up -d --build
conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/integration/test_full_workflow.py -v
conda run -n doubao-leads --no-capture-output npm.cmd --prefix frontend run e2e
```

Expected: FAIL until seeding, service composition, images, proxy, and routes are wired together.

- [ ] **Step 4: Implement deterministic seeding and the release verifier**

`seed_demo.py` creates one demo workspace, one administrator, one operator, one reviewer, masked synthetic provider credentials, and no verified business data. User passwords are supplied through one-run environment variables and are never written to generated files or output. The script is idempotent and refuses to run when `APP_ENV=production` unless `ALLOW_DEMO_SEED=true` is set explicitly.

`verify_release.py` performs these checks in order and exits nonzero on the first failure: UTF-8 decode and mojibake scan for Markdown, forbidden secret-pattern scan, Alembic single-head check, backend unit and integration tests, frontend tests, typecheck, production build, and Playwright workflow. It prints a compact JSON summary without environment values.

Update `compose.yaml` with health checks and dependency conditions for PostgreSQL, Redis, API, worker, and web. The web proxy serves the React build and forwards `/api/` to FastAPI. Document local conda commands, Docker Compose commands, demo limitations, credential entry, backup boundaries, and the requirement that only verified real data may be delivered.

Start the API container with `uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000` so production settings are read only when the process starts.

- [ ] **Step 5: Run the full release gate**

Run:

```bash
conda run -n doubao-leads --no-capture-output python scripts/verify_release.py
```

Expected: JSON output with `"status": "passed"`, zero secret findings, one Alembic head, all backend and frontend tests passing, a successful frontend build, and a passing Playwright workflow.

- [ ] **Step 6: Manually inspect desktop and mobile workflows**

Open the local application and verify at 1440 × 900 and 390 × 844:

- Login, sidebar or mobile navigation, and role visibility do not overlap.
- Long Chinese task and company names wrap without changing control dimensions.
- City progress rows remain readable and actions do not shift during status updates.
- Evidence links, review controls, export safety copy, and provider masks are visible.
- No synthetic record can trigger a formal export or real delivery.

- [ ] **Step 7: Commit the verified five-city closed loop**

```bash
git add compose.yaml .env.example README.md backend frontend scripts
git commit -m "feat: complete five-city lead workflow"
```

## Final Verification Checklist

- [ ] `git status --short` shows no unintended files.
- [ ] `git diff --check` reports no whitespace errors.
- [ ] `conda run -n doubao-leads --no-capture-output python scripts/verify_release.py` reports `passed`.
- [ ] Five cities and five default keyword groups appear in the immutable task plan.
- [ ] Every work unit has a terminal state or a visible attention state.
- [ ] Landlines are rejected and phone numbers are never inferred as WeChat IDs.
- [ ] Verified contacts include strong evidence URL, capture time, excerpt or object reference, and hash.
- [ ] Synthetic and pending records are absent from formal exports and real deliveries.
- [ ] Duplicate normalized contacts merge or create a visible company conflict.
- [ ] Cross-workspace API and file requests return 404.
- [ ] Credentials remain encrypted, masked, and absent from logs.
- [ ] City XLSX, CSV, task ZIP, local test delivery, and audit history are reproducible.

## Follow-On Plan Entry Criteria

A live-provider plan begins only after the provider's API or authorized-web documentation, account scope, rate limits, data-use terms, and test credentials are available. A target-system delivery plan begins only after its OpenAPI or equivalent contract, authentication method, field mapping, idempotency behavior, and test endpoint are available. Those plans reuse the adapter interfaces and safety tests delivered here without changing the verified-data rules.
