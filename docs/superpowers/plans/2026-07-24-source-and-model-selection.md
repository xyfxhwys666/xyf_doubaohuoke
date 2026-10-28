# Source And Model Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add selectable real sources, public-web contact enrichment, and optional single/multi-model evidence verification.

**Architecture:** Keep discovery providers and model verifiers as separate registries. Providers return source-grounded leads; verifiers annotate those leads without inventing fields. The task service combines verdicts into a deterministic consensus status.

**Tech Stack:** FastAPI, Pydantic, httpx, pytest, React, TypeScript, Vite.

## Global Constraints

- Formal leads require a source URL and at least one mobile, email, or WeChat contact.
- Models never create contacts absent from source evidence.
- A task requires at least one configured real source; models are optional.
- Existing downloads, pull API, push API, deduplication, retries, and nationwide execution remain compatible.

---

### Task 1: Public Web Source

**Files:**
- Create: `backend/app/collection/web_search.py`
- Modify: `backend/app/collection/providers.py`
- Modify: `backend/app/config.py`
- Test: `backend/tests/collection/test_web_search_provider.py`

**Interfaces:**
- Produces: `WebSearchProvider.collect(city, keyword, request_text) -> list[dict]`.

- [ ] Write tests proving search-result pages yield only source-linked mobile/email/WeChat contacts.
- [ ] Run the focused tests and confirm they fail because `WebSearchProvider` is absent.
- [ ] Implement HTML result parsing, bounded page fetching, contact extraction, and provider registration.
- [ ] Run focused tests and confirm they pass.

### Task 2: Model Registry And Verification

**Files:**
- Create: `backend/app/collection/models.py`
- Modify: `backend/app/config.py`
- Test: `backend/tests/collection/test_model_verification.py`

**Interfaces:**
- Produces: `ModelVerifier.verify(leads, request_text, city, keyword) -> list[dict]`.
- Produces: `apply_verification_consensus(leads, verdicts_by_model) -> list[dict]`.

- [ ] Write tests for configuration, JSON response parsing, single-model status, consensus, and disagreement.
- [ ] Run focused tests and confirm expected failures.
- [ ] Implement OpenAI-compatible DeepSeek/豆包 adapters and deterministic consensus.
- [ ] Run focused tests and confirm they pass.

### Task 3: Task API Integration

**Files:**
- Modify: `backend/app/main.py`
- Modify: `backend/app/collection/service.py`
- Modify: `backend/app/api/routes/collection_tasks.py`
- Modify: `backend/app/collection/exports.py`
- Test: `backend/tests/api/test_collection_mvp.py`

**Interfaces:**
- Consumes: provider and verifier registries from Tasks 1 and 2.
- Produces: `GET /api/v1/models` and task input field `models`.

- [ ] Write API tests for selectable sources, configured model metadata, validation, and persisted verification fields.
- [ ] Run focused API tests and confirm expected failures.
- [ ] Wire sources and models into task creation, execution, summaries, exports, pull, and push payloads.
- [ ] Run focused API tests and confirm they pass.

### Task 4: React Controls And Results

**Files:**
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/styles.css`

**Interfaces:**
- Consumes: `/providers`, `/models`, task `models`, and lead verification fields.

- [ ] Add independent source and model multi-select state.
- [ ] Show zero/single/multi-model mode and configuration status.
- [ ] Send selected sources/models and render evidence-based verification results.
- [ ] Run TypeScript typecheck and production build.

### Task 5: End-To-End Verification

**Files:**
- Modify: `.env.example`

- [ ] Run all backend tests and Ruff.
- [ ] Run frontend typecheck and build.
- [ ] Restart backend and frontend with persistent local processes.
- [ ] Verify the local page, provider/model controls, and API responses in the browser.
- [ ] Commit and push the completed branch.
