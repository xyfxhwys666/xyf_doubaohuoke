# Ark Web Search Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route official Doubao calls through Ark Responses API and use web search only for lead discovery and company enrichment.

**Architecture:** Keep `OpenAICompatibleVerifier` as the shared business gateway. Add one provider-aware JSON request method that emits either Ark Responses or OpenAI-compatible Chat Completions payloads, then have existing operations select whether external web search is required.

**Tech Stack:** Python 3.12, httpx, pytest, Pydantic settings, FastAPI application.

## Global Constraints

- Official Ark is identified by the `volces.com` base URL and an `ep-` endpoint model.
- DeepSeek and relay providers continue using `/chat/completions`.
- Only discovery and enrichment operations enable `web_search`.
- ERP evidence and delivery gates remain unchanged.
- API keys and Authorization headers never appear in test output or errors.

---

### Task 1: Ark Responses Transport

**Files:**
- Modify: `backend/app/collection/models.py`
- Test: `backend/tests/collection/test_model_verification.py`

**Interfaces:**
- Produces: `OpenAICompatibleVerifier._request_json(prompt, system_prompt, operation, use_web_search=False) -> dict[str, Any]`
- Produces: `OpenAICompatibleVerifier._extract_response_text(payload) -> str`
- Preserves: existing `OpenAICompatibleVerifier` constructor and public methods.

- [ ] **Step 1: Write failing protocol tests**

Add tests that construct an official Ark verifier, call `parse_intent_keywords`, and assert the request path is `/api/v3/responses`, the payload uses `input`, no `tools` field is present, and an `output` message containing `output_text` is parsed.

```python
def test_official_ark_uses_responses_api_without_web_search_for_intent() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert request.url.path == "/api/v3/responses"
        assert "input" in payload
        assert "tools" not in payload
        return httpx.Response(200, json={"output": [{"type": "message", "content": [
            {"type": "output_text", "text": '{"keywords":["软装公司"]}'}
        ]}]})
```

- [ ] **Step 2: Verify the test fails for the missing Responses path**

Run:

```text
C:\ProgramData\miniforge3\Scripts\conda.exe run -n doubao-leads --no-capture-output python -m pytest backend/tests/collection/test_model_verification.py -q --basetemp=.pytest-ark-red
```

Expected: FAIL because the current request path is `/chat/completions`.

- [ ] **Step 3: Implement the provider-aware request method**

Implement one method that sends official Ark requests as:

```python
payload = {
    "model": self.model,
    "input": [
        {"role": "system", "content": [{"type": "input_text", "text": system_prompt}]},
        {"role": "user", "content": [{"type": "input_text", "text": prompt}]},
    ],
}
```

Extract the first non-empty `output_text`. Keep the existing Chat Completions payload and parser for all non-Ark base URLs. Convert HTTP, shape, and JSON failures to `ModelVerificationError` without response bodies.

- [ ] **Step 4: Route intent parsing and verification through the shared method**

Replace their duplicate HTTP calls with `_request_json(..., use_web_search=False)` and keep all existing result validation.

- [ ] **Step 5: Run focused tests**

Expected: all model verification tests pass.

---

### Task 2: Web Search Selection for Collection Operations

**Files:**
- Modify: `backend/app/collection/models.py`
- Test: `backend/tests/collection/test_model_verification.py`

**Interfaces:**
- Changes: `_request_items(..., use_web_search: bool = False) -> list[dict[str, Any]]`
- Uses: Ark tool payload `{"type": "web_search", "max_keyword": 2, "limit": 10}` with `max_tool_calls=2`.

- [ ] **Step 1: Write failing web-search selection tests**

Add tests for `discover_companies`, `discover`, and `enrich_company` that assert official Ark requests include:

```python
assert payload["tools"] == [{"type": "web_search", "max_keyword": 2, "limit": 10}]
assert payload["max_tool_calls"] == 2
```

Retain or extend the existing relay test to assert `/chat/completions` remains in use.

- [ ] **Step 2: Verify the tests fail because tools are missing**

Run the focused test file with a fresh `--basetemp`. Expected: the new tool assertions fail.

- [ ] **Step 3: Enable web search only at external-fact call sites**

Pass `use_web_search=True` from `discover`, `discover_companies`, and `enrich_company`. Do not enable it for `parse_intent_keywords` or `_verify_batch`.

- [ ] **Step 4: Run focused and collection regression tests**

Run:

```text
C:\ProgramData\miniforge3\Scripts\conda.exe run -n doubao-leads --no-capture-output python -m pytest backend/tests/collection/test_model_verification.py backend/tests/collection/test_two_stage_service.py backend/tests/collection/test_continuous_collection.py -q --basetemp=.pytest-ark-green
```

Expected: all selected tests pass.

- [ ] **Step 5: Restart and health-check local services**

Use `.planning/restart_local_services.py`; verify backend and frontend health without making a billable model call because the account has no remaining quota.
