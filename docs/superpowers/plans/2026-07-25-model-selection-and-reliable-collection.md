# Model Selection And Reliable Collection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add explicit single/multi-model selection and prevent concurrent nationwide searches from causing widespread empty results.

**Architecture:** Pass selected model IDs from React to the natural-language endpoint, union keyword suggestions, batch verification calls, and serialize task execution with a service lock. Preserve the public-web evidence boundary for every saved contact.

**Tech Stack:** React, TypeScript, FastAPI, Python, pytest, `httpx`.

## Global Constraints

- Never persist model-only contact data without accessible public-web evidence.
- At least one configured model must be selected.
- Unconfigured models remain visible but disabled.
- One collection task performs web search at a time per backend process.

---

### Task 1: Explicit model selection contract

**Files:** `backend/app/api/routes/collection_tasks.py`, `backend/tests/api/test_collection_mvp.py`, `frontend/src/api.ts`, `frontend/src/types.ts`, `frontend/src/App.tsx`, `frontend/src/styles.css`

- [ ] Add failing API tests proving selected models override model words in natural language and multiple models are retained.
- [ ] Add `models` to the natural-language request and union keyword suggestions from selected models.
- [ ] Add frontend model loading, enabled/disabled multi-select controls, validation, and request payload.
- [ ] Run API tests and frontend build.

### Task 2: Reliable multi-model verification

**Files:** `backend/app/collection/models.py`, `backend/tests/collection/test_model_verification.py`

- [ ] Add a failing test where more leads than one response batch are verified completely.
- [ ] Split model verification into batches of at most 8 leads and preserve original indexes.
- [ ] Run model and collection tests.

### Task 3: Serialized task execution

**Files:** `backend/app/collection/service.py`, `backend/tests/api/test_collection_mvp.py`

- [ ] Add a failing concurrency test proving two tasks cannot enter providers simultaneously.
- [ ] Guard `run_task` with a service-level lock without changing persisted task results.
- [ ] Run API tests.

### Task 4: Verification and delivery

**Files:** all modified files

- [ ] Run complete related pytest suites, Ruff, and frontend build.
- [ ] Restart backend and frontend, verify health and model list.
- [ ] Run a small real task, inspect lead count and errors.
- [ ] Commit and push Gitee branch.
