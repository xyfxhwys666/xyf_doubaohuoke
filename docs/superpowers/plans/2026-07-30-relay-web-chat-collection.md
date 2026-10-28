# Relay And Doubao Web Chat Collection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore low-cost relay candidate discovery and use the persistent Doubao chat webpage for per-company contact enrichment without removing current evidence, deduplication, task-control, or ERP protections.

**Architecture:** Configure a separate OpenAI-compatible relay verifier for the `doubao` model. Hybrid tasks discover verified company candidates through that verifier, then route only contact enrichment through `DoubaoWebChatGateway`; browser login failures pause and resume from the saved company checkpoint.

**Tech Stack:** Python 3.12, FastAPI, Pydantic Settings, Playwright persistent context, React, pytest, Ruff.

## Global Constraints

- Never copy or inspect Codex in-app browser cookies or profile data.
- Keep official Ark Responses API out of the default hybrid path.
- Preserve public-evidence validity gates and global ERP deduplication.
- ERP automatic delivery remains off during tests.
- Web login/challenge pauses the task without losing its checkpoint.

---

### Task 1: Separate relay configuration

**Files:**
- Modify: `backend/app/config.py`
- Modify: `backend/app/collection/models.py`
- Modify: `.env.example`
- Test: `backend/tests/test_config.py`
- Test: `backend/tests/collection/test_model_verification.py`

- [ ] Write failing tests for `DOUBAO_RELAY_BASE_URL`, `DOUBAO_RELAY_API_KEY`, and `DOUBAO_RELAY_MODEL`, relay preference, and chat-completions payloads without web-search tools.
- [ ] Implement relay settings and build the `doubao` verifier from relay values when present.
- [ ] Keep official Doubao settings available only as explicit fallback configuration, not automatic fallback after relay failure.
- [ ] Run focused configuration and model tests.

### Task 2: Route hybrid collection correctly

**Files:**
- Modify: `backend/app/collection/service.py`
- Modify: `backend/app/api/schemas/collection.py`
- Test: `backend/tests/collection/test_two_stage_service.py`
- Test: `backend/tests/collection/test_continuous_collection.py`

- [ ] Write failing tests proving hybrid discovery calls the relay verifier and never calls webpage discovery.
- [ ] Write failing tests proving every verified candidate is enriched through `DoubaoWebChatGateway` and never through relay enrichment.
- [ ] Implement `web_chat_discovery=false` as the hybrid default while preserving the old explicit full-web mode internally.
- [ ] Verify login/challenge pause, resume at current company, skip-on-timeout, continuous rounds, and strict target stopping.

### Task 3: User-facing mode and runtime verification

**Files:**
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/types.ts`
- Modify: `README.md`

- [ ] Show the selected path as “中转发现 + 豆包网页查联系方式” when Doubao webpage is ready.
- [ ] Explain that Codex in-app browser login is for manual checks and the application browser login is required for multi-day automation.
- [ ] Run all backend tests, Ruff, frontend production build, and `git diff --check`.
- [ ] Restart local services with outbound network access and verify frontend/backend health.
- [ ] With ERP auto-push off, validate relay candidate discovery and stop before webpage login if the persistent application profile is not ready.
