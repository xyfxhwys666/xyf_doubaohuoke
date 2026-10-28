# Public Lead Expansion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Increase real public lead coverage per city while preventing script/CSS contamination and fabricated filler data.

**Architecture:** Extend the existing `WebSearchProvider` with deterministic HTML sanitization, contact-page discovery, and adaptive multi-query collection. Keep the existing service/model verification boundary and deduplication behavior.

**Tech Stack:** Python 3.12, FastAPI, `httpx`, standard-library `HTMLParser`, pytest, React/Vite.

## Global Constraints

- Never invent a company, phone number, email address, WeChat ID, address, or service description.
- Ten leads per city is a search target, not a guaranteed output count.
- If a city remains below target after all public search passes, save the real results and continue silently.
- Landline-only records remain excluded.

---

### Task 1: Clean public page text and service introductions

**Files:**
- Modify: `backend/app/collection/web_search.py`
- Test: `backend/tests/collection/test_web_search_provider.py`

**Interfaces:**
- Consumes: raw HTML and a search-result snippet.
- Produces: `_visible_text(html: str) -> str` without executable/template content and `clean_service_intro(...) -> str` capped at 180 characters.

- [ ] **Step 1: Write failing tests** for HTML containing `script`, `style`, CSS selectors, URLs, platform feedback text, and a valid Chinese service sentence.
- [ ] **Step 2: Run** `python -m pytest backend/tests/collection/test_web_search_provider.py -q` and confirm the new assertions fail because script text is retained.
- [ ] **Step 3: Implement ignored-tag depth in `_TextParser` and `_SearchResultParser`, then normalize the intro by removing URLs and code-like fragments while retaining Chinese business text.**
- [ ] **Step 4: Run the same test file and confirm it passes.**

### Task 2: Discover contact subpages and broaden search combinations

**Files:**
- Modify: `backend/app/collection/web_search.py`
- Modify: `backend/app/config.py`
- Test: `backend/tests/collection/test_web_search_provider.py`

**Interfaces:**
- Consumes: a candidate result URL, city, and keyword.
- Produces: source-linked lead dictionaries using the existing provider schema.

- [ ] **Step 1: Write failing tests** where the landing page contains no contact but links to `/contact`, and where a valid lead appears only in a later search expression.
- [ ] **Step 2: Run the focused tests and confirm zero leads are returned before implementation.**
- [ ] **Step 3: Add an HTML link parser limited to same-domain contact/about links and expand query generation to eight deterministic expressions for mobile, email, WeChat, official site, and public company directories.**
- [ ] **Step 4: Deduplicate candidate and evidence URLs before network requests; stop only after the configured expressions are exhausted or the target has been met and two consecutive expressions add nothing.**
- [ ] **Step 5: Run the provider tests and confirm contact evidence still requires a public source URL.**

### Task 3: Preserve city progress and verify the complete workflow

**Files:**
- Modify: `backend/app/collection/service.py` only if city-level continuation needs adjustment.
- Test: `backend/tests/api/test_collection_mvp.py`
- Verify: `frontend/src/App.tsx`

**Interfaces:**
- Consumes: zero or more verified leads per city/keyword unit.
- Produces: completed nationwide task progress without a blocking shortage message.

- [ ] **Step 1: Add an API regression test** proving a city with fewer than 10 real leads completes and the next city still runs.
- [ ] **Step 2: Run the API test and verify the behavior; make only the minimal service change if it fails.**
- [ ] **Step 3: Run** `python -m pytest backend/tests/api/test_collection_mvp.py backend/tests/collection/test_web_search_provider.py -q` and `python -m ruff check backend`.
- [ ] **Step 4: Run** `npm.cmd run build` in `frontend`.
- [ ] **Step 5: Restart the backend, run a real Wuhan collection, inspect source-linked samples and Chinese intros, then commit and push the branch.**
