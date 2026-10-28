# Two-Stage Company Enrichment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将模型直采改为“每城最多 100 家公司候选 + 单公司逐条补全”，并让候选、补全进度和每条结果实时保存展示。

**Architecture:** `OpenAICompatibleVerifier` 提供候选发现和单公司补全两个独立接口。`CollectionService` 先合并所选模型返回的候选，再使用最多 3 个工作线程逐家公司补全；每家公司完成后持久化任务，失败时按所选模型顺序降级。前端轮询候选和补全计数，候选即使没有公开联系方式也继续显示。

**Tech Stack:** Python 3、FastAPI、httpx、pytest、React、TypeScript、Vite

## Global Constraints

- 每个城市最多 100 家候选，多个关键词合并后按“城市 + 标准化公司名称”去重。
- 候选生成只返回公司名称、命中关键词、候选理由和置信度。
- 单公司补全每次请求只包含一家公司，最多并发 3 家。
- 不确定字段保存为 `null`，座机不得保存为手机号码。
- 每家公司处理完成立即保存；模型数据保持“待验证”。
- 单模型失败时按用户选择顺序尝试下一个模型；所有模型失败仍保留候选。
- 保存时间格式为 `YYYY-MM-DD HH:mm:ss`，不得写入编造数据。

---

### Task 1: Split Model Discovery And Enrichment APIs

**Files:**
- Modify: `backend/app/collection/models.py`
- Test: `backend/tests/collection/test_model_verification.py`

**Interfaces:**
- Consumes: configured OpenAI-compatible model endpoint and existing `ModelVerificationError`.
- Produces: `discover_companies(*, request_text: str, city: str, keywords: list[str], limit: int = 100) -> list[dict[str, Any]]` and `enrich_company(*, request_text: str, city: str, company: dict[str, Any], required_contacts: list[str]) -> dict[str, Any]`.

- [ ] **Step 1: Write failing model contract tests**

Add tests whose mock handler asserts the discovery prompt contains all keywords, asks for at most 100 names, and does not request contact fields. Add a second test whose handler asserts exactly one company name is present in the enrichment prompt and returns mobile/email/address/source data.

```python
companies = verifier.discover_companies(
    request_text="找武汉软装和整装公司",
    city="武汉",
    keywords=["软装公司", "整装公司"],
)
assert companies == [{
    "company_name": "武汉示例装饰有限公司",
    "matched_keyword": "整装公司",
    "reason": "提供整装服务",
    "source_confidence": 72,
}]

lead = verifier.enrich_company(
    request_text="找武汉软装和整装公司",
    city="武汉",
    company=companies[0],
    required_contacts=["mobile"],
)
assert lead["company_name"] == "武汉示例装饰有限公司"
assert lead["mobile"] == "13800138000"
```

- [ ] **Step 2: Run tests and observe the missing-method failure**

Run: `conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/collection/test_model_verification.py -q`

Expected: FAIL with `AttributeError` for `discover_companies` or `enrich_company`.

- [ ] **Step 3: Implement the two model calls**

Use a shared `_chat_json(prompt: str, system_prompt: str, timeout: int) -> list[dict[str, Any]]` parser. Discovery maps only the four candidate fields and caps results at `limit`; enrichment copies the input company name and maps nullable contact, address, introduction, source, and confidence fields. Both calls use temperature `0` and reject malformed JSON with `ModelVerificationError`.

- [ ] **Step 4: Run focused tests**

Run: `conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/collection/test_model_verification.py -q`

Expected: all tests pass.

### Task 2: Normalize And Deduplicate Company Candidates

**Files:**
- Modify: `backend/app/collection/normalization.py`
- Test: `backend/tests/api/test_collection_mvp.py`

**Interfaces:**
- Consumes: raw candidate dictionaries from model providers.
- Produces: `normalize_company_name(value: object) -> str | None` and `deduplicate_company_candidates(candidates: Iterable[dict[str, Any]]) -> list[dict[str, Any]]`.

- [ ] **Step 1: Write failing candidate normalization tests**

```python
assert normalize_company_name("武汉某某装饰有限公司") == "武汉某某装饰有限公司"
assert normalize_company_name("武汉软装公司排行榜") is None
assert len(deduplicate_company_candidates([
    {"city": "武汉", "company_name": "武汉某某装饰有限公司"},
    {"city": "武汉", "company_name": " 武汉某某装饰有限公司 "},
])) == 1
```

- [ ] **Step 2: Run tests and observe import failure**

Run: `conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/api/test_collection_mvp.py -q`

Expected: FAIL because the new normalization functions do not exist.

- [ ] **Step 3: Implement conservative name filtering and city-aware deduplication**

Trim whitespace and punctuation, reject empty/generic/search-result titles containing `排行榜`、`招聘`、`哪家好`、`推荐`、`电话`、`联系方式`, require a company-like suffix or a substantial brand name, and deduplicate by normalized city plus company name while keeping the higher-confidence candidate.

- [ ] **Step 4: Run the focused API tests**

Run: `conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/api/test_collection_mvp.py -q`

Expected: all tests pass.

### Task 3: Execute Candidate And Enrichment Phases With Immediate Persistence

**Files:**
- Modify: `backend/app/collection/service.py`
- Test: `backend/tests/api/test_collection_mvp.py`

**Interfaces:**
- Consumes: `discover_companies(...)`, `enrich_company(...)`, `deduplicate_company_candidates(...)`, selected model order, task contact rules.
- Produces: task fields `collection_phase`, `candidate_target`, `candidate_count`, `enrichment_total`, `enrichment_completed`; persisted lead fields `enrichment_state` and existing lead schema.

- [ ] **Step 1: Write failing service behavior tests**

Add deterministic fake verifiers that return multiple candidates and record concurrent enrichment calls. Assert candidates are saved before enrichment completes, at most three enrichment calls are active, completed companies persist immediately, a failing first model falls back to the second, a landline is cleared, and rerunning an interrupted task skips `enrichment_state == "completed"` companies.

```python
task = service.get_task(task_id)
assert task["candidate_count"] == 4
assert task["enrichment_total"] == 4
assert task["enrichment_completed"] == 4
assert all(item["verification_status"] == "model_discovered" for item in task["leads"])
assert probe.max_active <= 3
```

- [ ] **Step 2: Run tests and observe missing progress/fallback behavior**

Run: `conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/api/test_collection_mvp.py -q`

Expected: FAIL because collection still uses the one-shot `discover()` flow and lacks enrichment counters.

- [ ] **Step 3: Refactor `CollectionService` into two phases**

For each city, call every selected configured model for a combined keyword list, merge and cap candidates to the parsed per-city limit or 100, create candidate lead rows with nullable fields and `enrichment_state="pending"`, and save immediately. Use `ThreadPoolExecutor(max_workers=3)` for single-company enrichment. For each completed future, merge the returned public fields, normalize contacts, set `enrichment_state` to `completed` or `failed`, increment counters, deduplicate strong contact identifiers, update timestamps, and call `TaskStore.save()` while holding the existing lock.

- [ ] **Step 4: Preserve restart and error semantics**

Recovery keeps saved candidates, computes the pending company list from `enrichment_state`, retries each model once before fallback, records errors without removing candidates, and completes the city even when fewer than 100 real companies are returned.

- [ ] **Step 5: Run backend tests**

Run: `conda run -n doubao-leads --no-capture-output python -m pytest backend/tests -q`

Expected: all tests pass.

### Task 4: Show Two-Phase Live Progress In React

**Files:**
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/App.tsx`

**Interfaces:**
- Consumes: task summary fields `collection_phase`, `candidate_target`, `candidate_count`, `enrichment_total`, `enrichment_completed`.
- Produces: two stable progress rows and lead polling keyed by candidate/enrichment counts.

- [ ] **Step 1: Add TypeScript fields and UI assertions through the build**

Extend `CollectionTask` with exact numeric fields and phase union `'discovering' | 'enriching' | 'completed'`. Update `leadRefreshKey` to include both counters so candidate-only saves refresh the table.

- [ ] **Step 2: Render candidate and enrichment progress**

Display `候选公司 candidate_count / candidate_target` and `信息补全 enrichment_completed / enrichment_total`; label `pending` rows as `待补全` and retain `未公开` for nullable fields.

- [ ] **Step 3: Run the frontend build**

Run: `conda run -n doubao-leads --no-capture-output npm.cmd --prefix frontend run build`

Expected: TypeScript and Vite build exit with code 0.

### Task 5: End-To-End Verification And Delivery

**Files:**
- Verify: `backend/app/collection/models.py`
- Verify: `backend/app/collection/service.py`
- Verify: `frontend/src/App.tsx`

**Interfaces:**
- Consumes: complete two-stage implementation.
- Produces: verified branch commit pushed to Gitee.

- [ ] **Step 1: Run lint, backend tests, and frontend build**

Run: `conda run -n doubao-leads --no-capture-output python -m ruff check backend`

Run: `conda run -n doubao-leads --no-capture-output python -m pytest backend/tests -q`

Run: `conda run -n doubao-leads --no-capture-output npm.cmd --prefix frontend run build`

Expected: every command exits with code 0.

- [ ] **Step 2: Restart local services and verify health**

Run: `conda run -n doubao-leads --no-capture-output python scripts/restart_local_server.py`

Expected: backend health returns 200 at `http://127.0.0.1:8010/health` and frontend loads at `http://127.0.0.1:5173/`.

- [ ] **Step 3: Run one-city real-model smoke test**

Create a Wuhan task using one configured model, confirm candidate rows appear before all enrichments finish, confirm no landline is stored in `mobile`, and confirm each returned source remains `待验证` until the user triggers verification.

- [ ] **Step 4: Commit and push**

```bash
git add backend frontend docs/superpowers/plans/2026-07-27-two-stage-company-enrichment.md
git commit -m "feat: add two-stage company enrichment"
git push origin codex/five-city-lead-platform
```
