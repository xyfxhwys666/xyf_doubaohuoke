# Daily Verified Target Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make continuous collection stop at 100 strictly verified leads by default while preserving explicit user limits and existing evidence gates.

**Architecture:** Store a dedicated `valid_lead_target` on each task instead of overloading the per-city candidate limit. The continuous runner checks this target before discovery and after verification, and the frontend reports strict progress separately from candidate volume.

**Tech Stack:** Python 3.12, FastAPI, React, TypeScript, pytest, Ruff.

## Global Constraints

- Never reuse or inspect Codex in-app browser cookies.
- Count only leads accepted by `verified_erp_ineligibility()`.
- Preserve ERP auto-push default off and global deduplication.
- User-specified quantities override the default 100 target.

---

### Task 1: Strict target defaults

**Files:**
- Modify: `backend/app/collection/continuous.py`
- Modify: `backend/app/collection/service.py`
- Test: `backend/tests/collection/test_continuous_collection.py`

**Interfaces:**
- Produces: `DEFAULT_VALID_LEAD_TARGET: int` and persisted `valid_lead_target` task field.

- [ ] Write failing tests for default target 100, explicit target preservation, and no discovery after the target is already reached.
- [ ] Run the focused tests and confirm failures are caused by the missing target field.
- [ ] Implement the task default and switch continuous stopping checks to `valid_lead_target`.
- [ ] Run focused tests and the complete backend suite.

### Task 2: Visible strict progress

**Files:**
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/types.ts`

**Interfaces:**
- Consumes: `valid_lead_target` and `contact_lead_count` from task summaries.

- [ ] Add task typing for `valid_lead_target`.
- [ ] Change continuous-mode copy to describe the 100 strict-valid target.
- [ ] Display current strict-valid count against the target in task progress.
- [ ] Run the frontend production build and inspect the local page.

### Task 3: Release verification

**Files:**
- Verify: all changed backend, frontend, and documentation files.

**Interfaces:**
- Produces: a tested commit on `codex/five-city-lead-platform`.

- [ ] Run all backend tests, Ruff, frontend build, UTF-8 checks, and `git diff --check`.
- [ ] Restart local services and verify backend/frontend health.
- [ ] Commit and push the feature branch without staging local test artifacts or secrets.
