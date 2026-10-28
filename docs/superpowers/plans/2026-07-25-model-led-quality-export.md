# Model-Led Quality Export Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver model-led evidence filtering, required-contact controls, stable source collection, clean company names, and selectable XLSX/CSV/JSON exports.

**Architecture:** Discovery providers only return source-grounded evidence. Selected models must unanimously approve each lead before it becomes formal output, while optional AMap evidence supplies standardized business details. Export formatting is isolated from collection and writes explicit unpublished values.

**Tech Stack:** FastAPI, Pydantic, httpx, pytest, React, TypeScript, Vite.

## Global Constraints

- At least one configured model and one real source are required.
- Every selected contact field must be present.
- Models cannot invent companies or contacts.
- AMap remains optional.
- Empty cities are excluded from ZIP exports.

---

### Task 1: Formal Lead Qualification

**Files:**
- Modify: `backend/app/api/routes/collection_tasks.py`
- Modify: `backend/app/collection/service.py`
- Modify: `backend/app/collection/normalization.py`
- Test: `backend/tests/api/test_collection_mvp.py`

**Interfaces:**
- Produces: task field `required_contacts: list[Literal["mobile", "email", "wechat"]]`.

- [ ] Add failing API tests for required contacts, mandatory model selection, unanimous model acceptance, and rejection removal.
- [ ] Run focused tests and confirm the expected failures.
- [ ] Implement validation and formal-lead filtering.
- [ ] Run focused tests and confirm they pass.

### Task 2: Stable Web Evidence And Names

**Files:**
- Modify: `backend/app/collection/web_search.py`
- Test: `backend/tests/collection/test_web_search_provider.py`

**Interfaces:**
- Produces: cleaned source title and retrying, throttled search requests.

- [ ] Add failing tests for title noise removal and retryable rate-limit responses.
- [ ] Implement bounded retry/backoff and company-name cleanup.
- [ ] Run focused tests and confirm they pass.

### Task 3: Export Formats

**Files:**
- Modify: `backend/app/collection/exports.py`
- Modify: `backend/app/api/routes/collection_tasks.py`
- Test: `backend/tests/api/test_collection_mvp.py`

**Interfaces:**
- Produces: export formats `xlsx`, `csv`, and `json`.

- [ ] Add failing tests for JSON, unpublished placeholders, and omission of empty cities.
- [ ] Implement consistent serialization for all formats.
- [ ] Run export tests and confirm they pass.

### Task 4: Frontend Controls

**Files:**
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/styles.css`

**Interfaces:**
- Consumes: `required_contacts`, model metadata, and export format.

- [ ] Add contact requirement checkboxes and validate at least one selection.
- [ ] Display all known model choices, with unconfigured models disabled.
- [ ] Add one export-format selector and download action.
- [ ] Render missing contacts as “未公开”.

### Task 5: Release Verification

**Files:**
- Modify: `.env.example` only if configuration metadata changes.

- [ ] Run all backend tests and Ruff.
- [ ] Run frontend production build.
- [ ] Restart services and verify the page and API endpoints.
- [ ] Run a small real-data task and inspect formal output quality.
- [ ] Commit and push the branch.
