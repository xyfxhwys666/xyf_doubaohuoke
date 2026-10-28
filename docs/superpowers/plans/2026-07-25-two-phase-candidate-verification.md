# Two Phase Candidate Verification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show model and web candidates immediately, then verify only when the user clicks a button.

**Architecture:** Separate candidate discovery from verification in `CollectionService`. Add a verification endpoint and React control while preserving existing lead schemas and exports.

**Tech Stack:** FastAPI, Python, React, TypeScript, pytest.

## Global Constraints

- Model-only candidates are always labeled unverified.
- Verification never deletes candidates.
- Candidate contact requirements and deduplication remain active.

---

### Task 1: Persist unverified model and web candidates

**Files:** `backend/app/collection/service.py`, `backend/tests/api/test_collection_mvp.py`

- [ ] Add failing tests for model candidates surviving web-source failure and automatic verification not running.
- [ ] Collect from selected model `discover` methods, normalize, filter contacts, deduplicate, and save as unverified.
- [ ] Run API tests.

### Task 2: Add explicit verification endpoint

**Files:** `backend/app/collection/service.py`, `backend/app/api/routes/collection_tasks.py`, `backend/tests/api/test_collection_mvp.py`

- [ ] Add failing tests proving verification updates statuses without deleting rows.
- [ ] Add service verification workflow and `POST /verify` endpoint.
- [ ] Run API and model tests.

### Task 3: Add verification button

**Files:** `frontend/src/api.ts`, `frontend/src/types.ts`, `frontend/src/App.tsx`, `frontend/src/styles.css`

- [ ] Add API client call and task verification state types.
- [ ] Add a result-panel button with running, completed, and retry states.
- [ ] Build frontend.

### Task 4: Verify and deliver

- [ ] Run pytest, Ruff, and frontend build.
- [ ] Restart services and run a real small task.
- [ ] Commit and push Gitee branch.
