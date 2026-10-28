# High-Yield Verified Leads Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Continuously collect many real companies with a publicly evidenced mainland mobile number, automatically keep the strongest number per company, and prevent duplicate ERP customers across tasks and restarts.

**Architecture:** Add a focused evidence scorer and persistent delivery ledger beside the existing collection service. Extend official Ark web-search enrichment into forward company search, alias search, and reverse-mobile confirmation; the service accepts A/B evidence, queues C evidence for manual review, and keeps expanding candidates until the strict target is met.

**Tech Stack:** Python 3.12, FastAPI, Pydantic, httpx, JSON persistence, React, TypeScript, Vitest, pytest, Ruff.

## Global Constraints

- A valid automated lead must contain a complete real company entity and a valid 11-digit mainland mobile tied to it by public evidence.
- Model-only answers never auto-pass or auto-push.
- Address, service description, contact name, and role are optional.
- Keep one highest-quality mobile per canonical company.
- ERP automatic delivery remains disabled during real-search validation.
- Default candidate expansion is at most 20 candidates per requested valid lead.
- A failed or slow company must not block the remaining queue.

---

### Task 1: Evidence grading and best-number selection

**Files:**
- Create: `backend/app/collection/evidence.py`
- Modify: `backend/app/collection/normalization.py`
- Test: `backend/tests/collection/test_evidence_scoring.py`

**Interfaces:**
- Produces: `EvidenceGrade`, `ContactEvidence`, `grade_contact_evidence(company, mobile, sources)`, `choose_best_contact(company, contacts)`.
- Consumes: normalized company names, mobile numbers, source URLs, quotes, and optional contact person metadata.

- [ ] **Step 1: Write failing tests for A-grade same-source evidence, B-grade independent forward/reverse closure, model-only C-grade evidence, conflicts, and multiple-number ranking.**

- [ ] **Step 2: Run `python -m pytest backend/tests/collection/test_evidence_scoring.py -q` and confirm imports or assertions fail.**

- [ ] **Step 3: Implement immutable evidence records, source-domain independence, authority scoring, exact company/mobile matching, reverse-match scoring, and deterministic best-number selection.**

- [ ] **Step 4: Integrate the selector into `deduplicate_leads()` so duplicate company records merge evidence and retain the strongest phone instead of the first phone.**

- [ ] **Step 5: Run evidence and normalization tests and confirm they pass.**

### Task 2: Three-stage Doubao web-search enrichment

**Files:**
- Modify: `backend/app/collection/models.py`
- Modify: `backend/app/collection/doubao_web.py`
- Modify: `backend/app/collection/service.py`
- Test: `backend/tests/collection/test_doubao_web.py`
- Test: `backend/tests/collection/test_two_stage_service.py`

**Interfaces:**
- Consumes: verified canonical company, aliases, city, keywords, and desired contact fields.
- Produces: evidence candidates from legal-name search, alias search, and reverse-mobile search, then an A/B/C verdict.

- [ ] **Step 1: Write failing tests asserting legal-name search runs first, alias search fills missing phones, every discovered phone receives reverse search, and one company timeout does not stop later candidates.**

- [ ] **Step 2: Run the focused tests and confirm the new query and verdict expectations fail.**

- [ ] **Step 3: Extend Ark `/responses` web-search prompts and citation parsing to return source URL, quote, matched name or alias, mobile, and query stage without trusting uncited JSON fields.**

- [ ] **Step 4: Add per-company query budget, timeout handling, retry/backoff for throttling, and negative-result caching; failed companies return C-grade or rejected records and release the worker slot.**

- [ ] **Step 5: Pass collected evidence through Task 1 grading, clear mobiles below B grade from automatic-valid records, and retain them in manual-review metadata.**

- [ ] **Step 6: Run the focused model/service tests and the existing collection suite.**

### Task 3: Persistent global deduplication and ERP idempotency

**Files:**
- Create: `backend/app/collection/dedup_ledger.py`
- Modify: `backend/app/collection/erp_delivery.py`
- Modify: `backend/app/collection/service.py`
- Test: `backend/tests/collection/test_dedup_ledger.py`
- Test: `backend/tests/collection/test_erp_delivery.py`

**Interfaces:**
- Produces: `DedupLedger.lookup()`, `reserve()`, `mark_delivered()`, and `release()` using atomic JSON replacement and a process lock.
- Consumes: normalized mobile, canonical company key, aliases, ERP GUID, evidence score, payload fingerprint, and delivery timestamp.

- [ ] **Step 1: Write failing tests for same-mobile skip across tasks, same-company skip after restart, stable ERP GUID, better-number update, concurrent reservation, and retry after failed delivery.**

- [ ] **Step 2: Run focused ledger and ERP tests and confirm they fail.**

- [ ] **Step 3: Implement the ledger schema with mobile and company indexes, atomic persistence, corruption recovery, and stable GUID generation from canonical company identity.**

- [ ] **Step 4: Make ERP delivery reserve before POST, mark only successful responses delivered, release failed reservations, skip equal/worse evidence, and update the original GUID only for a stronger number.**

- [ ] **Step 5: Run focused tests twice with a fresh temporary store to verify restart behavior and idempotency.**

### Task 4: Continuous expansion and non-blocking collection

**Files:**
- Modify: `backend/app/collection/intent.py`
- Modify: `backend/app/collection/continuous.py`
- Modify: `backend/app/collection/service.py`
- Test: `backend/tests/collection/test_intent_parser.py`
- Test: `backend/tests/collection/test_continuous_collection.py`
- Test: `backend/tests/collection/test_task_scheduler.py`

**Interfaces:**
- Consumes: requested city, customer description, strict valid target, processed company keys, and query history.
- Produces: district-by-keyword query batches and checkpointed rounds until target, pause, or stop.

- [ ] **Step 1: Write failing tests for 20x candidate budget, district and business-synonym expansion, query-history exclusion, skipped failures, and exact strict-target stopping.**

- [ ] **Step 2: Run focused continuous tests and confirm the expansion expectations fail.**

- [ ] **Step 3: Generate deterministic district/keyword query groups, persist processed company/query keys in checkpoints, and refill the candidate queue whenever remaining candidates cannot satisfy the target.**

- [ ] **Step 4: Add bounded concurrency and per-company deadlines while preserving pause, resume, stop, and checkpoint semantics.**

- [ ] **Step 5: Run continuous and scheduler tests, including resume after application restart.**

### Task 5: Manual confirmation fallback and release verification

**Files:**
- Modify: `backend/app/collection/routes.py`
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/styles.css`
- Test: `backend/tests/collection/test_routes.py`
- Test: `frontend/src/App.test.tsx`

**Interfaces:**
- Produces: `POST /tasks/{task_id}/leads/{lead_id}/confirm-and-deliver` with audit actor, timestamp, and note; UI row action only for C-grade records.
- Consumes: existing ERP configuration and Task 3 global dedup rules.

- [ ] **Step 1: Write failing API and UI tests for C-grade action visibility, confirmation audit, successful push, duplicate skip, and failed push status.**

- [ ] **Step 2: Implement the endpoint and UI action with explicit confirmation and row-level pending/success/failure state.**

- [ ] **Step 3: Run backend tests, `python -m ruff check backend`, frontend tests, and frontend production build.**

- [ ] **Step 4: Restart local services, verify health endpoints and browser controls, then run a small real collection with ERP auto-push off.**

- [ ] **Step 5: Run a 30-valid-target Shenzhen test, inspect every accepted company/mobile evidence link, and run a second overlapping task to prove global deduplication.**
