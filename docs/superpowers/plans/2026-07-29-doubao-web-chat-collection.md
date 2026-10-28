# Doubao Web Chat Collection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a persistent, resumable Doubao web-chat collection channel that discovers real companies and enriches contacts while allowing ERP delivery only after official-page or independent cross-source verification.

**Architecture:** Introduce a queue-backed browser actor that exclusively owns a persistent Playwright Chrome context and exposes a synchronous gateway to the existing collection service. Keep Doubao responses as candidates only; reuse and extend the existing public-evidence verifier, task checkpoints, continuous scheduler, and ERP eligibility gate. The frontend shows browser/login state separately from model selection and lets the user resume after re-authentication.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy/Alembic, Playwright sync API, httpx, pytest, React 19, TypeScript, Vite.

## Global Constraints

- Do not call the Doubao API or Web Search plugin in web-chat mode.
- Use the user's local persistent browser profile; never store passwords or display cookies.
- Do not bypass CAPTCHA, login gates, access control, or paid data restrictions.
- Doubao output is candidate data, never verification evidence.
- Only `verified_official_page` and `verified_cross_source` company/contact records may enter the verified ERP path.
- A mainland mobile must match `^1[3-9]\d{9}$`; landlines and 400 numbers are not mobile contacts.
- A login or challenge pauses the task as `waiting_for_login`; re-authentication resumes from the current company checkpoint.
- Existing API-based models remain verification choices and are not silently replaced by the web-chat source.
- Continuous mode, task isolation, downloads, pause/resume/terminate, and ERP idempotency must not regress.
- Runtime and test commands use the `doubao-leads` conda environment.

---

### Task 1: Persist web-chat and multi-source evidence state

**Files:**
- Modify: `backend/app/models/collection.py`
- Create: `backend/migrations/versions/0004_doubao_web_chat.py`
- Modify: `backend/app/collection/service.py`
- Modify: `backend/app/collection/normalization.py`
- Modify: `backend/tests/models/test_collection_models.py`
- Modify: `backend/tests/collection/test_two_stage_service.py`

**Interfaces:**
- Produces task fields `use_doubao_web_chat`, `web_chat_status`, `web_chat_error`, `web_chat_current_company`, and `web_chat_checkpoint`.
- Produces lead fields `contact_evidence_urls: list[str]` and `contact_evidence_quotes: list[str]` while retaining singular fields for ERP/export compatibility.
- Defines verification statuses `verified_official_page`, `verified_cross_source`, `pending_login_source`, `unverified_model_only`, `rejected_mismatch`, and `verification_failed`.

- [ ] **Step 1: Write failing persistence/default tests**

Add tests that create a task and lead without the new fields, then assert backward-compatible defaults:

```python
assert task["use_doubao_web_chat"] is False
assert task["web_chat_status"] == "disabled"
assert task["web_chat_error"] is None
assert task["web_chat_current_company"] is None
assert task["web_chat_checkpoint"] == {}
assert lead["contact_evidence_urls"] == []
assert lead["contact_evidence_quotes"] == []
```

- [ ] **Step 2: Run the focused tests and confirm RED**

Run:

```powershell
conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/models/test_collection_models.py backend/tests/collection/test_two_stage_service.py -q
```

Expected: failures for missing task and lead fields.

- [ ] **Step 3: Add model columns and migration**

Add task columns:

```python
use_doubao_web_chat: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
web_chat_status: Mapped[str] = mapped_column(String(32), nullable=False, default="disabled")
web_chat_error: Mapped[str | None] = mapped_column(Text)
web_chat_current_company: Mapped[str | None] = mapped_column(String(320))
web_chat_checkpoint: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
```

Add lead columns:

```python
contact_evidence_urls: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
contact_evidence_quotes: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
```

Migration `0004_doubao_web_chat.py` must add the columns with server defaults, backfill existing rows, then remove JSON/string server defaults where the existing migration style does so.

- [ ] **Step 4: Add dictionary defaults and normalization**

Extend `TASK_DEFAULTS`, lead defaults, task summary, and normalization. Preserve singular evidence aliases:

```python
if evidence_urls and not normalized.get("contact_evidence_url"):
    normalized["contact_evidence_url"] = evidence_urls[0]
if evidence_quotes and not normalized.get("contact_evidence_quote"):
    normalized["contact_evidence_quote"] = evidence_quotes[0]
```

- [ ] **Step 5: Run focused tests and confirm GREEN**

Run the Step 2 command. Expected: all selected tests pass.

- [ ] **Step 6: Commit Task 1**

```powershell
git add backend/app/models/collection.py backend/migrations/versions/0004_doubao_web_chat.py backend/app/collection/service.py backend/app/collection/normalization.py backend/tests/models/test_collection_models.py backend/tests/collection/test_two_stage_service.py
git commit -m "feat: persist doubao web chat state"
```

---

### Task 2: Build deterministic prompts and response parsing

**Files:**
- Create: `backend/app/collection/doubao_web.py`
- Create: `backend/tests/collection/test_doubao_web.py`

**Interfaces:**
- Produces `DoubaoWebCandidate`, `DoubaoWebContact`, and `DoubaoWebResponseError`.
- Produces `build_discovery_prompt(...)`, `build_contact_prompt(...)`, `parse_discovery_response(...)`, and `parse_contact_response(...)`.
- Parsing returns candidates only and does not assign a verified status.

- [ ] **Step 1: Write parser tests for JSON and visible pipe output**

Cover the two formats observed in the live Doubao page:

```python
def test_parse_discovery_pipe_rows_keeps_source_and_quote() -> None:
    text = "1｜深圳市测试设计有限公司｜https://example.cn/about｜公司原文包含别墅全案"
    result = parse_discovery_response(text, city="深圳市")
    assert result[0].company_name == "深圳市测试设计有限公司"
    assert result[0].source_urls == ["https://example.cn/about"]
    assert result[0].evidence_quotes == ["公司原文包含别墅全案"]

def test_parse_contact_rejects_landline_and_model_only_mobile() -> None:
    text = "深圳市测试设计有限公司｜0755-12345678｜未提供来源｜联系电话"
    result = parse_contact_response(text, company_name="深圳市测试设计有限公司")
    assert result.mobile is None
    assert result.source_urls == []
```

Also cover fenced JSON, Unicode hyphens in formatted mobile numbers, duplicate URLs, mismatched company names, `未找到可核验公开手机号`, and malformed output.

- [ ] **Step 2: Run parser tests and confirm RED**

```powershell
conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/collection/test_doubao_web.py -q
```

Expected: import failure because `doubao_web.py` does not exist.

- [ ] **Step 3: Implement immutable result types and prompt builders**

Use dataclasses:

```python
@dataclass(frozen=True, slots=True)
class DoubaoWebCandidate:
    company_name: str
    city: str
    matched_keyword: str
    source_urls: list[str]
    evidence_quotes: list[str]
    raw_response: str

@dataclass(frozen=True, slots=True)
class DoubaoWebContact:
    company_name: str
    mobile: str | None
    contact_person_name: str | None
    contact_person_role: str | None
    email: str | None
    wechat: str | None
    address: str | None
    service_intro: str | None
    source_urls: list[str]
    evidence_quotes: list[str]
    raw_response: str
```

Discovery prompts request at most five companies and forbid login-only or model-only evidence. Contact prompts request exactly one company and explicitly return `null`/`未公开` when no same-page mobile exists.

- [ ] **Step 4: Implement defensive parsers**

Normalize mobile with `re.sub(r"\D", "", value)`, accept it only when the mainland pattern matches, normalize URLs with `urlparse`, and require the requested company in the parsed row. Never synthesize missing URLs or quotes.

- [ ] **Step 5: Run parser tests and confirm GREEN**

Run the Step 2 command. Expected: all tests pass.

- [ ] **Step 6: Commit Task 2**

```powershell
git add backend/app/collection/doubao_web.py backend/tests/collection/test_doubao_web.py
git commit -m "feat: parse doubao web chat results"
```

---

### Task 3: Add the persistent browser actor

**Files:**
- Modify: `backend/pyproject.toml`
- Modify: `environment.yml`
- Create: `backend/app/collection/doubao_browser.py`
- Create: `backend/tests/collection/test_doubao_browser.py`

**Interfaces:**
- Produces exceptions `DoubaoLoginRequired`, `DoubaoChallengeRequired`, `DoubaoWebUnavailable`, and `DoubaoWebTimeout`.
- Produces `DoubaoBrowserStatus(state, detail, current_company)`.
- Produces `DoubaoWebChatGateway.start()`, `.status()`, `.discover_companies(...)`, `.enrich_company(...)`, and `.shutdown()`.

- [ ] **Step 1: Write actor tests with an injected fake adapter**

The fake adapter must record that all commands execute on one actor thread, preserve a profile path, and return scripted page text. Assert:

```python
assert gateway.status().state == "ready"
assert adapter.thread_ids == {adapter.owner_thread_id}
assert gateway.discover_companies(...)[0].company_name == "深圳市测试设计有限公司"
```

Add tests that visible text containing `登录以解锁更多功能`, `扫码登录`, or `安全验证` raises the corresponding exception without retrying or bypassing it. Add a shutdown test that drains no new commands after closure.

- [ ] **Step 2: Run actor tests and confirm RED**

```powershell
conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/collection/test_doubao_browser.py -q
```

- [ ] **Step 3: Add Playwright dependency**

Add `playwright>=1.54,<2` to backend dependencies and conda/pip environment metadata. Runtime uses installed Chrome via `channel="chrome"`; do not download or downgrade Node/Python as a workaround.

- [ ] **Step 4: Implement a single-owner queue actor**

Use `queue.Queue`, `threading.Thread`, and a command future/event. Only the actor thread may create or touch Playwright objects:

```python
class DoubaoWebChatGateway:
    def _actor_main(self) -> None:
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir),
                channel="chrome",
                headless=False,
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(self.chat_url)
            self._consume_commands(page)
```

Do not expose cookies or storage state through API responses or logs.

- [ ] **Step 5: Implement stable response waiting and checkpoint-safe errors**

Locate the visible textarea by the known placeholders, send one prompt, then poll the final conversation text until it is unchanged for three consecutive polls and generation controls disappear. Cap each request with the configured timeout. Detect login/challenge before every new prompt and after navigation.

- [ ] **Step 6: Run actor and parser tests**

```powershell
conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/collection/test_doubao_browser.py backend/tests/collection/test_doubao_web.py -q
```

Expected: all tests pass without opening a real browser because tests inject the fake adapter.

- [ ] **Step 7: Commit Task 3**

```powershell
git add backend/pyproject.toml environment.yml backend/app/collection/doubao_browser.py backend/tests/collection/test_doubao_browser.py
git commit -m "feat: add persistent doubao browser worker"
```

---

### Task 4: Configure and expose web-chat runtime status

**Files:**
- Modify: `.env.example`
- Modify: `backend/app/config.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/api/routes/collection_tasks.py`
- Modify: `backend/tests/test_config.py`
- Modify: `backend/tests/api/test_collection_mvp.py`

**Interfaces:**
- Consumes `DoubaoWebChatGateway` from Task 3.
- Produces `GET /api/v1/collection-tasks/doubao-web/status`.
- Produces `POST /api/v1/collection-tasks/doubao-web/start` to open/reuse the login browser without starting a collection task.

- [ ] **Step 1: Write configuration and API tests**

Assert defaults are disabled and secrets are absent from responses:

```python
assert settings.doubao_web_enabled is False
assert settings.doubao_web_chat_url == "https://www.doubao.com/chat/"
assert response.json() == {
    "enabled": True,
    "state": "waiting_for_login",
    "detail": "需要登录豆包网页",
    "current_company": None,
}
assert "cookie" not in response.text.lower()
```

- [ ] **Step 2: Run tests and confirm RED**

```powershell
conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/test_config.py backend/tests/api/test_collection_mvp.py -q
```

- [ ] **Step 3: Add settings and sanitized example values**

```env
DOUBAO_WEB_ENABLED=false
DOUBAO_WEB_PROFILE_DIR=./data/doubao-browser-profile
DOUBAO_WEB_CHAT_URL=https://www.doubao.com/chat/
DOUBAO_WEB_REQUEST_TIMEOUT_SECONDS=180
```

Resolve relative profile paths under the project data directory. Do not add profile data to Git; extend `.gitignore` for `data/doubao-browser-profile/`.

- [ ] **Step 4: Construct and shut down the gateway with the app lifespan**

Create the gateway only when enabled. Inject it into `CollectionService`; invoke `shutdown()` after the scheduler stops. API handlers return only the sanitized `DoubaoBrowserStatus` fields.

- [ ] **Step 5: Run focused tests and commit**

Run the Step 2 command, then:

```powershell
git add .env.example .gitignore backend/app/config.py backend/app/main.py backend/app/api/routes/collection_tasks.py backend/tests/test_config.py backend/tests/api/test_collection_mvp.py
git commit -m "feat: expose doubao browser status"
```

---

### Task 5: Integrate browser discovery/enrichment and login recovery

**Files:**
- Modify: `backend/app/collection/service.py`
- Modify: `backend/app/collection/scheduler.py`
- Modify: `backend/app/api/routes/collection_tasks.py`
- Modify: `backend/tests/collection/test_two_stage_service.py`
- Modify: `backend/tests/collection/test_task_scheduler.py`
- Modify: `backend/tests/api/test_collection_task_controls.py`

**Interfaces:**
- Consumes `DoubaoWebChatGateway` and its typed exceptions.
- Produces task state `waiting_for_login` and resume behavior through the existing resume endpoint.
- Produces service control exception `TaskWaitingForLogin`, handled separately from `TaskPaused` so scheduler cleanup cannot overwrite the login state.
- Uses `use_doubao_web_chat=true` as an explicit per-task flag; model selection remains unchanged.

- [ ] **Step 1: Write failing service and scheduler tests**

Cover these scenarios with a fake gateway:

```python
def test_web_chat_discovers_in_batches_of_five_and_enriches_sequentially(...): ...
def test_login_required_preserves_company_checkpoint(...): ...
def test_resume_continues_current_company_without_repeating_previous_company(...): ...
def test_terminate_from_waiting_for_login_keeps_results(...): ...
def test_api_models_are_not_changed_when_web_chat_is_enabled(...): ...
def test_scheduler_does_not_convert_waiting_for_login_to_paused(...): ...
```

Assert the checkpoint includes city, batch number, company index, and processed normalized names.

- [ ] **Step 2: Run focused tests and confirm RED**

```powershell
conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/collection/test_two_stage_service.py backend/tests/collection/test_task_scheduler.py backend/tests/api/test_collection_task_controls.py -q
```

- [ ] **Step 3: Route company discovery through the explicit web-chat source**

When `use_doubao_web_chat` is true, call the gateway in batches of five and persist raw candidates before verification. Do not call `OpenAICompatibleVerifier.discover_companies` as a fallback if the browser is unavailable; pause or record an error instead.

- [ ] **Step 4: Enrich verified companies sequentially**

Do not submit web-chat enrichment to the existing three-worker executor. For web-chat tasks, iterate one company at a time, save after each response, then call the contact evidence verifier. API-mode tasks retain the existing executor behavior.

- [ ] **Step 5: Implement `waiting_for_login` transitions**

On login/challenge exceptions:

```python
task["status"] = "waiting_for_login"
task["web_chat_status"] = "waiting_for_login"
task["web_chat_error"] = str(exc)
task["web_chat_current_company"] = company_name
self._save_progress(task)
raise TaskWaitingForLogin(str(exc))
```

Handle `TaskWaitingForLogin` in `_run_scheduled_task_locked()` without calling `_mark_paused()`. Resume must first verify `gateway.status().state == "ready"`; otherwise return a state error without losing the checkpoint.

- [ ] **Step 6: Run focused tests and commit**

Run the Step 2 command, then:

```powershell
git add backend/app/collection/service.py backend/app/collection/scheduler.py backend/app/api/routes/collection_tasks.py backend/tests/collection/test_two_stage_service.py backend/tests/collection/test_task_scheduler.py backend/tests/api/test_collection_task_controls.py
git commit -m "feat: run collection through doubao web chat"
```

---

### Task 6: Add official-page and independent cross-source verification

**Files:**
- Modify: `backend/app/collection/web_search.py`
- Modify: `backend/app/collection/continuous.py`
- Modify: `backend/app/collection/exports.py`
- Modify: `backend/app/collection/erp_delivery.py`
- Modify: `backend/tests/collection/test_web_search_provider.py`
- Modify: `backend/tests/collection/test_continuous_collection.py`
- Modify: `backend/tests/collection/test_erp_delivery.py`

**Interfaces:**
- Consumes candidate/contact source URL lists from Tasks 1-2.
- Produces `verified_official_page` or `verified_cross_source` only from fetched page content.
- `verified_erp_ineligibility()` accepts only those two statuses for both company and contact.

- [ ] **Step 1: Write failing verification tests**

Create scripted responses for:

```python
def test_official_page_requires_exact_company_and_mobile_same_page(...): ...
def test_cross_source_requires_two_distinct_domains(...): ...
def test_same_domain_pages_do_not_count_as_two_sources(...): ...
def test_matching_doubao_quote_without_fetched_page_is_unverified(...): ...
def test_login_page_becomes_pending_login_source(...): ...
def test_conflicting_mobiles_are_rejected(...): ...
```

Extend ERP tests so both accepted statuses deliver, while `pending_login_source`, `unverified_model_only`, and `rejected_mismatch` never call the sender.

- [ ] **Step 2: Run focused tests and confirm RED**

```powershell
conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/collection/test_web_search_provider.py backend/tests/collection/test_continuous_collection.py backend/tests/collection/test_erp_delivery.py -q
```

- [ ] **Step 3: Implement evidence classification**

For each URL, store normalized domain, fetch result, exact company-name match, exact mobile match, quote, and capture time. Official-page success requires one source with both matches. Cross-source success requires two distinct domains with both matches and identical normalized mobile.

- [ ] **Step 4: Update ERP gate and export schema**

Replace the old single status checks with:

```python
VERIFIED_EVIDENCE_STATUSES = {"verified_official_page", "verified_cross_source"}
```

Export comma-separated evidence URLs in CSV and JSON arrays in JSON/XLSX while retaining the first URL in the legacy ERP mapping.

- [ ] **Step 5: Run focused tests and commit**

Run the Step 2 command, then:

```powershell
git add backend/app/collection/web_search.py backend/app/collection/continuous.py backend/app/collection/exports.py backend/app/collection/erp_delivery.py backend/tests/collection/test_web_search_provider.py backend/tests/collection/test_continuous_collection.py backend/tests/collection/test_erp_delivery.py
git commit -m "feat: verify contacts with independent evidence"
```

---

### Task 7: Add frontend browser/login and evidence controls

**Files:**
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/styles.css`
- Modify: `frontend/package.json` only if the existing test runner needs a script alias
- Create or modify: `frontend/src/App.test.tsx`

**Interfaces:**
- Consumes sanitized web-chat status and task fields from Tasks 4-5.
- Produces a task-level `use_doubao_web_chat` toggle, browser status panel, login/start command, and resume action.
- Displays official-page and cross-source labels plus all evidence links.

- [ ] **Step 1: Write failing component/API tests**

Assert:

```tsx
expect(screen.getByText('豆包聊天网页')).toBeInTheDocument()
expect(screen.getByText('需要重新登录')).toBeInTheDocument()
expect(screen.getByRole('button', { name: '打开登录窗口' })).toBeEnabled()
expect(screen.getByText('官网同页验证')).toBeInTheDocument()
expect(screen.getByText('双来源验证')).toBeInTheDocument()
```

Also assert model checkboxes are unchanged and unconfigured API models remain disabled.

- [ ] **Step 2: Run frontend tests and confirm RED**

```powershell
conda run -n doubao-leads --no-capture-output npm.cmd --prefix frontend test -- --run
```

- [ ] **Step 3: Extend types and API calls**

Add `waiting_for_login` to `TaskStatus`, browser state types, multi-source evidence arrays, `use_doubao_web_chat` to task creation, and status/start methods to `api.ts`.

- [ ] **Step 4: Implement restrained operational UI**

Place the web-chat source row below the natural-language input and above model selection. Use a checkbox for enabling it, a status tag, and an icon button/text command for opening the login window. Do not display cookies, profile paths, passwords, or instructional feature copy.

In the results table, map:

```typescript
verified_official_page: '官网同页验证'
verified_cross_source: '双来源验证'
pending_login_source: '登录来源待核验'
unverified_model_only: '仅模型回答'
rejected_mismatch: '主体或号码冲突'
```

- [ ] **Step 5: Run tests and production build**

```powershell
conda run -n doubao-leads --no-capture-output npm.cmd --prefix frontend test -- --run
conda run -n doubao-leads --no-capture-output npm.cmd --prefix frontend run build
```

Expected: tests pass and Vite build succeeds.

- [ ] **Step 6: Commit Task 7**

```powershell
git add frontend/src/types.ts frontend/src/api.ts frontend/src/App.tsx frontend/src/styles.css frontend/src/App.test.tsx frontend/package.json
git commit -m "feat: show doubao browser collection status"
```

---

### Task 8: End-to-end recovery and live acceptance

**Files:**
- Modify: `backend/tests/api/test_collection_mvp.py`
- Modify: `backend/tests/api/test_collection_task_controls.py`
- Modify: `docs/superpowers/specs/2026-07-29-doubao-web-chat-collection-design.md` only if implementation reveals a confirmed contract correction
- Create: `docs/doubao-web-chat-operations.md`

**Interfaces:**
- Verifies the complete workflow without real external calls in automated tests.
- Documents the one-time login and recovery procedure for the local operator.

- [ ] **Step 1: Add an end-to-end fake-gateway test**

The test must create a natural-language task with `use_doubao_web_chat=true`, discover candidates, reject a mismatched source, pause for login on a later company, resume, verify a same-page mobile, and assert only the verified record reaches the fake ERP sender.

- [ ] **Step 2: Run the complete backend suite and lint**

```powershell
conda run -n doubao-leads --no-capture-output python -m pytest backend/tests -q
conda run -n doubao-leads --no-capture-output python -m ruff check backend/app backend/tests
```

Expected: all tests pass and Ruff reports no errors.

- [ ] **Step 3: Run frontend tests and build**

```powershell
conda run -n doubao-leads --no-capture-output npm.cmd --prefix frontend test -- --run
conda run -n doubao-leads --no-capture-output npm.cmd --prefix frontend run build
```

- [ ] **Step 4: Write UTF-8 operations documentation**

Document: enabling the feature, opening the persistent browser, completing login, starting a task, interpreting verification levels, resuming after login expiry, and stopping the browser worker. Explicitly state that CAPTCHA bypass is unsupported and that model-only contacts are not ERP eligible.

- [ ] **Step 5: Perform live five-company acceptance before the 30-company run**

With ERP auto delivery disabled:

1. Start the backend and frontend in `doubao-leads`.
2. Open the persistent login browser and confirm `ready` status.
3. Submit the Shenzhen villa soft-design request with a five-company target.
4. Confirm every accepted company has fetched evidence.
5. Confirm every accepted mobile has official-page or two-domain evidence.
6. Terminate the task and confirm all results remain downloadable.

Do not claim acceptance if login, CAPTCHA, or public-source access prevents evidence verification.

- [ ] **Step 6: Run the 30-company validation with ERP disabled**

Continue until 30 company names pass the evidence gate or the available sources are exhausted. Record verified mobiles as found; keep missing mobiles as `未公开`. The acceptance result must report total candidates examined, rejected names, verified companies, verified mobiles, login interruptions, and elapsed time.

- [ ] **Step 7: Commit Task 8**

```powershell
git add backend/tests/api/test_collection_mvp.py backend/tests/api/test_collection_task_controls.py docs/doubao-web-chat-operations.md docs/superpowers/specs/2026-07-29-doubao-web-chat-collection-design.md
git commit -m "test: validate doubao web chat workflow"
```

---

## Final Verification

- [ ] Run backend tests: `conda run -n doubao-leads --no-capture-output python -m pytest backend/tests -q`
- [ ] Run backend lint: `conda run -n doubao-leads --no-capture-output python -m ruff check backend/app backend/tests`
- [ ] Run frontend tests: `conda run -n doubao-leads --no-capture-output npm.cmd --prefix frontend test -- --run`
- [ ] Run frontend build: `conda run -n doubao-leads --no-capture-output npm.cmd --prefix frontend run build`
- [ ] Confirm `.env`, browser profile, cookies, logs containing raw sessions, and API keys are not staged.
- [ ] Confirm ERP auto delivery remains disabled during five-company and 30-company acceptance runs.
