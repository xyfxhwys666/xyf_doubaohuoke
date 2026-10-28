# Contact Evidence Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 只保留能在可重新访问的公开网页正文中同时核对公司全称和手机号的联系信息，并将公开联系人、职务和证据状态展示、导出和用于 ERP 推送门槛。

**Architecture:** 豆包逐家补全返回结构化联系候选及来源 URL；`PublicWebSearchProvider` 作为独立证据验证器重新抓取来源正文；`CollectionService` 只把验证通过的手机号写入最终线索。新字段通过 JSON 文件存储、SQLAlchemy 模型、API、导出和 React 表格全链路传递。

**Tech Stack:** Python 3.12, FastAPI, httpx, SQLAlchemy, Alembic, pytest, React, TypeScript, Vite.

## Global Constraints

- 没有可重新访问的 HTTP(S) 证据页面，联系方式不得标记为已验证。
- 来源页面正文必须同时包含规范化公司全称和相同的 11 位大陆手机号。
- 座机、脱敏号码、相似公司、模型推测和不可访问页面均不得通过。
- 联系人姓名和职务只在来源正文包含完整原文时保存，否则留空。
- 公司名称或手机证据未通过的数据不得自动推送 ERP。
- 不实现微信搜索、微信昵称推断或个人资料抓取。

---

### Task 1: Public Web Contact Evidence Verifier

**Files:**
- Modify: `backend/app/collection/web_search.py`
- Test: `backend/tests/collection/test_web_search_provider.py`

**Interfaces:**
- Produces: `ContactEvidence(status, evidence_url, evidence_quote, reason, contact_person_name, contact_person_role)`
- Produces: `PublicWebSearchProvider.verify_contact_evidence(*, company_name: str, mobile: str | None, source_url: str | None, contact_person_name: str | None, contact_person_role: str | None) -> ContactEvidence`

- [ ] **Step 1: Write failing same-page evidence tests**

```python
def test_contact_evidence_accepts_exact_company_and_mobile_on_source_page() -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
        200,
        text="<p>深圳市甲软装设计有限公司</p><p>业务负责人：张三 13800138000</p>",
        request=request,
    )))
    provider = PublicWebSearchProvider(client=client, request_interval_seconds=0)
    evidence = provider.verify_contact_evidence(
        company_name="深圳市甲软装设计有限公司",
        mobile="13800138000",
        source_url="https://company.example/contact",
        contact_person_name="张三",
        contact_person_role="业务负责人",
    )
    assert evidence.status == "verified_public_web"
    assert evidence.contact_person_name == "张三"
    assert evidence.contact_person_role == "业务负责人"
    assert "13800138000" in evidence.evidence_quote
```

同文件增加以下拒绝用例：无 URL、URL 非 HTTP(S)、页面无完整公司名、页面无相同手机号、只有座机、抓取异常，以及姓名/职务未出现时仅清空两字段但保留手机验证通过。

- [ ] **Step 2: Run tests and verify RED**

Run: `C:\ProgramData\miniforge3\Scripts\conda.exe run -n doubao-leads --no-capture-output python -m pytest backend/tests/collection/test_web_search_provider.py -q`

Expected: FAIL because `ContactEvidence` and `verify_contact_evidence` do not exist.

- [ ] **Step 3: Implement the evidence value object and verifier**

```python
@dataclass(frozen=True)
class ContactEvidence:
    status: str
    evidence_url: str | None
    evidence_quote: str
    reason: str
    contact_person_name: str | None = None
    contact_person_role: str | None = None

def verify_contact_evidence(... ) -> ContactEvidence:
    # Validate URL and mobile, fetch with _get(), convert HTML through _visible_text(),
    # compare whitespace-normalized exact company name and exact normalized mobile,
    # and create a bounded quote around the matched mobile.
```

状态必须为 `verified_public_web`、`unverified`、`rejected` 或 `verification_failed`。

- [ ] **Step 4: Run provider tests and verify GREEN**

Run: `C:\ProgramData\miniforge3\Scripts\conda.exe run -n doubao-leads --no-capture-output python -m pytest backend/tests/collection/test_web_search_provider.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/collection/web_search.py backend/tests/collection/test_web_search_provider.py
git commit -m "feat: verify public contact evidence"
```

### Task 2: Doubao Structured Contact Candidate and Lead Fields

**Files:**
- Modify: `backend/app/collection/models.py`
- Modify: `backend/app/collection/normalization.py`
- Modify: `backend/app/collection/service.py`
- Test: `backend/tests/collection/test_model_verification.py`
- Test: `backend/tests/collection/test_candidate_normalization.py`

**Interfaces:**
- Consumes: `ContactEvidence` from Task 1.
- Produces lead keys: `contact_person_name`, `contact_person_role`, `contact_verification_status`, `contact_evidence_url`, `contact_evidence_quote`, `contact_verified_at`, `contact_verification_reason`.

- [ ] **Step 1: Write failing model and normalization tests**

```python
assert enriched == {
    **existing_fields,
    "contact_person_name": "张三",
    "contact_person_role": "业务负责人",
    "contact_evidence_url": "https://company.example/contact",
    "contact_evidence_quote": "深圳市甲软装设计有限公司 业务负责人张三 13800138000",
}
```

验证 `normalize_lead()` 为旧数据提供 `unverified` 默认值，并确保去重合并保留一组来自同一证据的联系人字段。

- [ ] **Step 2: Run focused tests and verify RED**

Run: `C:\ProgramData\miniforge3\Scripts\conda.exe run -n doubao-leads --no-capture-output python -m pytest backend/tests/collection/test_model_verification.py backend/tests/collection/test_candidate_normalization.py -q`

Expected: FAIL because the enrichment prompt/parser and normalized lead do not expose the new fields.

- [ ] **Step 3: Tighten the Doubao prompt and parse structured evidence**

`enrich_company()` 的 JSON 契约改为：

```json
{"items":[{"company_name":"原公司全称","mobile":null,"contact_person_name":null,"contact_person_role":null,"email":null,"wechat":null,"address":null,"service_intro":null,"source_url":null,"evidence_quote":null,"confidence":70}]}
```

提示词明确要求：仅提取联网页面原文，来源 URL 和原文必填，不得推测，找不到时字段为 `null`。

- [ ] **Step 4: Add defaults and atomic deduplication merging**

`normalize_lead()` 将新字段规范化；`deduplicate_leads()` 必须从同一最富候选中一次性复制手机、联系人、职务和联系证据，不允许跨来源拼接。

- [ ] **Step 5: Run focused tests and verify GREEN**

Run: `C:\ProgramData\miniforge3\Scripts\conda.exe run -n doubao-leads --no-capture-output python -m pytest backend/tests/collection/test_model_verification.py backend/tests/collection/test_candidate_normalization.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/collection/models.py backend/app/collection/normalization.py backend/app/collection/service.py backend/tests/collection/test_model_verification.py backend/tests/collection/test_candidate_normalization.py
git commit -m "feat: collect structured contact evidence"
```

### Task 3: Enrichment Evidence Gate and ERP Eligibility

**Files:**
- Modify: `backend/app/collection/service.py`
- Modify: `backend/app/collection/continuous.py`
- Test: `backend/tests/collection/test_two_stage_service.py`
- Test: `backend/tests/collection/test_continuous_collection.py`

**Interfaces:**
- Consumes: `verify_contact_evidence()` from Task 1 and normalized keys from Task 2.
- Produces: only a `verified_public_web` mobile remains eligible for display as verified and ERP delivery.

- [ ] **Step 1: Write failing service gate tests**

```python
def test_two_stage_enrichment_clears_mobile_when_public_page_does_not_confirm_it(...):
    # The fake Doubao returns 13800138000, while the contact verifier returns rejected.
    service.run(task_id)
    lead = store.list_leads(task_id)[0]
    assert lead["mobile"] is None
    assert lead["contact_verification_status"] == "rejected"
    assert lead["delivery_status"] == "display_only"
```

另加通过用例，确认同页证据保留手机、姓名、职务和证据链接；验证请求异常只影响当前公司。

- [ ] **Step 2: Run focused tests and verify RED**

Run: `C:\ProgramData\miniforge3\Scripts\conda.exe run -n doubao-leads --no-capture-output python -m pytest backend/tests/collection/test_two_stage_service.py backend/tests/collection/test_continuous_collection.py -q`

Expected: FAIL because enrichment currently trusts the model's mobile and ERP eligibility has no contact-evidence condition.

- [ ] **Step 3: Apply the verifier before persisting enriched contacts**

Add a `ContactVerifier` protocol and invoke it after Doubao enrichment normalization. On success, update all contact evidence fields. On every non-success status, clear `mobile`, `contact_person_name`, and `contact_person_role`; retain status, reason and safe evidence URL for audit.

- [ ] **Step 4: Tighten verified ERP eligibility**

```python
if lead.get("contact_verification_status") != "verified_public_web":
    return "contact_unverified"
```

Use `contact_evidence_url` rather than the model-supplied generic `source_url` for the HTTP(S) evidence check.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run: `C:\ProgramData\miniforge3\Scripts\conda.exe run -n doubao-leads --no-capture-output python -m pytest backend/tests/collection/test_two_stage_service.py backend/tests/collection/test_continuous_collection.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/collection/service.py backend/app/collection/continuous.py backend/tests/collection/test_two_stage_service.py backend/tests/collection/test_continuous_collection.py
git commit -m "feat: gate contacts on public evidence"
```

### Task 4: Persistence, API, Exports, and Compatibility

**Files:**
- Modify: `backend/app/models/collection.py`
- Create: `backend/migrations/versions/0003_contact_evidence.py`
- Modify: `backend/app/collection/exports.py`
- Modify: `backend/tests/api/test_collection_mvp.py`
- Modify: `backend/tests/models/test_collection_models.py`

**Interfaces:**
- Consumes the seven lead keys defined in Task 2.
- Produces backward-compatible API and XLSX/CSV/JSON fields with `unverified` defaults.

- [ ] **Step 1: Write failing persistence and export contract tests**

Add the following ordered fields to the API/export contract immediately after `mobile`:

```python
"contact_person_name",
"contact_person_role",
"contact_verification_status",
"contact_evidence_url",
"contact_evidence_quote",
"contact_verified_at",
"contact_verification_reason",
```

Assert a SQLAlchemy round trip preserves all values and old rows deserialize with `contact_verification_status == "unverified"`.

- [ ] **Step 2: Run focused tests and verify RED**

Run: `C:\ProgramData\miniforge3\Scripts\conda.exe run -n doubao-leads --no-capture-output python -m pytest backend/tests/models/test_collection_models.py backend/tests/api/test_collection_mvp.py -q`

Expected: FAIL because columns and export fields do not exist.

- [ ] **Step 3: Add nullable columns and migration**

The migration adds nullable text/string/timestamp columns and a non-null `contact_verification_status` with server default `unverified`; downgrade removes only these seven columns.

- [ ] **Step 4: Extend every export format**

Add the fields to `RESULT_FIELDS`; render absent person, role, URL and quote as `未公开`, absent status as `未验证`, and keep machine-readable stored/API status values unchanged.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run: `C:\ProgramData\miniforge3\Scripts\conda.exe run -n doubao-leads --no-capture-output python -m pytest backend/tests/models/test_collection_models.py backend/tests/api/test_collection_mvp.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/models/collection.py backend/migrations/versions/0003_contact_evidence.py backend/app/collection/exports.py backend/tests/models/test_collection_models.py backend/tests/api/test_collection_mvp.py
git commit -m "feat: persist and export contact evidence"
```

### Task 5: React Evidence Display and Release Verification

**Files:**
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/styles.css`

**Interfaces:**
- Consumes the API lead fields from Task 4.
- Produces visible columns: `联系人`, `职务`, `手机核验`, and `联系证据`.

- [ ] **Step 1: Add the TypeScript lead contract**

```ts
contact_person_name: string | null
contact_person_role: string | null
contact_verification_status: 'verified_public_web' | 'unverified' | 'rejected' | 'verification_failed'
contact_evidence_url: string | null
contact_evidence_quote: string
contact_verified_at: string | null
contact_verification_reason: string
```

- [ ] **Step 2: Update the results table**

Display `公开网页已核验`、`未验证`、`已拒绝` or `核验失败`; expose the reason as a tooltip and render a `查看证据` link only for HTTP(S) URLs. Increase the empty row `colSpan` to match the new column count.

- [ ] **Step 3: Run frontend production build**

Run: `C:\ProgramData\miniforge3\Scripts\conda.exe run -n doubao-leads --no-capture-output npm.cmd run build`

Working directory: `frontend`

Expected: TypeScript and Vite build succeed with no errors.

- [ ] **Step 4: Run backend quality gates**

Run: `C:\ProgramData\miniforge3\Scripts\conda.exe run -n doubao-leads --no-capture-output python -m pytest backend/tests -q`

Run: `C:\ProgramData\miniforge3\Scripts\conda.exe run -n doubao-leads --no-capture-output python -m ruff check backend/app backend/tests`

Expected: all tests and Ruff pass.

- [ ] **Step 5: Restart and test the local application**

After stopping the existing local development processes, start the backend from `backend`:

`C:\ProgramData\miniforge3\Scripts\conda.exe run -n doubao-leads --no-capture-output python -m uvicorn app.main:app --host 127.0.0.1 --port 8010 --reload`

Start the frontend from `frontend` in a separate terminal:

`C:\ProgramData\miniforge3\Scripts\conda.exe run -n doubao-leads --no-capture-output npm.cmd run dev -- --host 127.0.0.1 --port 5173`

Verify `http://127.0.0.1:8010/health` returns `{"status":"ok"}` and `http://127.0.0.1:5173/` displays the new evidence columns without overlap. Create a one-company test task and confirm that a model-only phone is not retained, while a same-page verified phone is displayed as `公开网页已核验`.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/types.ts frontend/src/App.tsx frontend/src/styles.css
git commit -m "feat: show contact evidence status"
```

- [ ] **Step 7: Final repository check**

Run: `git status --short --branch`

Expected: only pre-existing untracked test artifact directories remain; no tracked implementation file is unstaged.
