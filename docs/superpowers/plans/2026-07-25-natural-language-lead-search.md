# Natural Language Lead Search Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the multi-control task form with one natural-language request that automatically plans and runs evidence-backed public-web lead collection.

**Architecture:** A deterministic parser handles explicit cities, models, contact semantics, limits, and defaults; the selected model supplements keywords and structured intent. The task service always uses the hidden public-web provider for evidence, then applies model relevance scoring and deterministic cleaning before saving leads.

**Tech Stack:** FastAPI, Pydantic, httpx, pytest, React, TypeScript, Vite.

## Global Constraints

- No AMap data may participate in city expansion, collection, scoring, or frontend options.
- Unspecified cities mean the bundled nationwide tier-three-and-above city catalog.
- Unspecified model means Doubao; unavailable Doubao falls back to configured DeepSeek.
- Unspecified contacts mean mobile; multiple mentioned contacts use any-match unless the text explicitly requires all.
- Models cannot create companies or contacts absent from public-web evidence.
- Leads below confidence 50 are hidden from results, exports, and delivery.
- Saved timestamps use China Standard Time in `YYYY-MM-DD HH:MM:SS` format.
- Unspecified result count continues until two consecutive search pages add no qualified lead.

---

### Task 1: Natural-Language Intent Parser

**Files:**
- Create: `backend/app/collection/intent.py`
- Test: `backend/tests/collection/test_intent_parser.py`
- Modify: `backend/app/collection/models.py`

**Interfaces:**
- Produces: `ParsedCollectionIntent` with `cities`, `all_cities`, `keywords`, `models`, `required_contacts`, `contact_match`, and `limit_per_city`.
- Produces: `OpenAICompatibleVerifier.parse_intent(request_text: str) -> dict[str, Any]`.

- [ ] **Step 1: Write failing parser tests**

Cover these exact cases:

```python
def test_defaults_to_all_cities_doubao_and_mobile():
    intent = parse_collection_intent("寻找全案软装公司", configured_models={"doubao"})
    assert intent.all_cities is True
    assert intent.models == ["doubao"]
    assert intent.required_contacts == ["mobile"]
    assert intent.contact_match == "all"

def test_explicit_contacts_default_to_any_match():
    intent = parse_collection_intent("找武汉软装公司，要手机、邮箱或微信", configured_models={"doubao"})
    assert intent.cities == ["武汉市"]
    assert intent.required_contacts == ["mobile", "email", "wechat"]
    assert intent.contact_match == "any"

def test_explicit_all_contacts_uses_all_match():
    intent = parse_collection_intent("找长沙整装公司，手机和邮箱都要", configured_models={"doubao"})
    assert intent.contact_match == "all"

def test_model_and_limit_are_extracted():
    intent = parse_collection_intent("用 DeepSeek 查成都设计公司，只要 10 家", configured_models={"deepseek"})
    assert intent.models == ["deepseek"]
    assert intent.limit_per_city == 10
```

- [ ] **Step 2: Run parser tests and verify RED**

Run: `C:\ProgramData\miniforge3\envs\doubao-leads\python.exe -m pytest tests/collection/test_intent_parser.py -q`

Expected: import failure for `app.collection.intent`.

- [ ] **Step 3: Implement deterministic defaults and model-assisted keywords**

Implement:

```python
@dataclass(slots=True)
class ParsedCollectionIntent:
    cities: list[str]
    all_cities: bool
    keywords: list[str]
    models: list[str]
    required_contacts: list[Literal["mobile", "email", "wechat"]]
    contact_match: Literal["any", "all"]
    limit_per_city: int | None

def parse_collection_intent(
    request_text: str,
    *,
    configured_models: set[str],
    model_keywords: list[str] | None = None,
) -> ParsedCollectionIntent:
    text = request_text.strip()
    cities = extract_explicit_cities(text)
    contacts = extract_contacts(text) or ["mobile"]
    match = "all" if len(contacts) == 1 or contains_all_match_phrase(text) else "any"
    models = extract_models(text, configured_models) or default_model(configured_models)
    keywords = merge_keywords(extract_rule_keywords(text), model_keywords or [])
    return ParsedCollectionIntent(cities, not cities, keywords, models, contacts, match, extract_limit(text))
```

Use explicit dictionaries for `手机/电话`, `邮箱/邮件`, `微信/微信号`, model names, all-match phrases, and Chinese city aliases. Model output may add normalized keywords but cannot override explicit deterministic fields.

- [ ] **Step 4: Add and test model JSON intent parsing**

The model prompt must request only:

```json
{"keywords":["全案软装公司","整装公司"]}
```

Invalid or unavailable model output returns deterministic keywords instead of failing task creation.

- [ ] **Step 5: Run focused tests and commit**

Run parser and model tests. Commit: `feat: parse natural-language collection intent`.

### Task 2: Built-In Nationwide City Catalog

**Files:**
- Create: `backend/app/collection/cities.py`
- Create: `backend/app/collection/data/tier3_plus_cities.json`
- Test: `backend/tests/collection/test_city_catalog.py`
- Modify: `backend/app/collection/service.py`

**Interfaces:**
- Produces: `tier3_plus_city_names() -> list[str]`.
- Consumes: `ParsedCollectionIntent.all_cities` from Task 1.

- [ ] **Step 1: Add failing catalog tests**

```python
def test_catalog_is_stable_and_contains_representative_city_tiers():
    cities = tier3_plus_city_names()
    assert len(cities) == len(set(cities))
    assert {"北京市", "武汉市", "长沙市", "洛阳市", "绵阳市"} <= set(cities)
    assert all(city.endswith(("市", "自治州", "地区", "盟")) for city in cities)
```

- [ ] **Step 2: Run test and verify RED**

Expected: missing `app.collection.cities`.

- [ ] **Step 3: Add the bundled UTF-8 JSON catalog and loader**

The JSON file is a versioned product asset containing the approved tier-three-and-above city snapshot. The loader must UTF-8 decode, preserve order, deduplicate, and return a copy.

```python
def tier3_plus_city_names() -> list[str]:
    path = Path(__file__).parent / "data" / "tier3_plus_cities.json"
    values = json.loads(path.read_text(encoding="utf-8"))
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))
```

- [ ] **Step 4: Replace `CollectionService.city_names()` AMap dependency**

`city_names()` must return `tier3_plus_city_names()` without reading provider configuration.

- [ ] **Step 5: Run tests and commit**

Commit: `feat: bundle nationwide target city catalog`.

### Task 3: Natural-Language Task API and Contact Semantics

**Files:**
- Modify: `backend/app/api/routes/collection_tasks.py`
- Modify: `backend/app/collection/service.py`
- Test: `backend/tests/api/test_collection_mvp.py`

**Interfaces:**
- Produces: `POST /api/v1/collection-tasks/from-natural-language`.
- Consumes: `parse_collection_intent()` and `tier3_plus_city_names()`.

- [ ] **Step 1: Add failing API tests**

Test that a payload `{"request_text":"找武汉长沙软装公司，手机或邮箱，用 DeepSeek"}` creates a draft task with:

```python
assert task["cities"] == ["武汉市", "长沙市"]
assert task["keywords"] == ["软装公司"]
assert task["providers"] == ["web_search"]
assert task["models"] == ["deepseek"]
assert task["contact_match"] == "any"
```

Also test omitted settings produce all cities, Doubao, mobile, and `contact_match="all"`.

- [ ] **Step 2: Run API tests and verify RED**

Expected: HTTP 404 for the new route.

- [ ] **Step 3: Implement the request schema and endpoint**

```python
class NaturalLanguageCollectionTaskRequest(BaseModel):
    request_text: str = Field(min_length=1, max_length=4000)

@router.post("/collection-tasks/from-natural-language", status_code=201)
def create_natural_language_task(payload, request):
    service = _service(request)
    configured = {item["id"] for item in service.model_items() if item["configured"]}
    intent = parse_collection_intent(payload.request_text, configured_models=configured)
    task_payload = {
        "name": payload.request_text[:40],
        "request_text": payload.request_text,
        "cities": intent.cities,
        "all_cities": intent.all_cities,
        "keywords": intent.keywords,
        "providers": ["web_search"],
        "models": intent.models,
        "required_contacts": intent.required_contacts,
        "contact_match": intent.contact_match,
        "limit_per_city": intent.limit_per_city,
    }
    if intent.all_cities:
        task_payload["cities"] = tier3_plus_city_names()
    return service.task_summary(service.create_task(task_payload))
```

The endpoint selects a configured parser model, applies deterministic fallback, sets provider to `web_search`, expands all cities from the built-in catalog, and stores the parsed plan in the task activity log.

- [ ] **Step 4: Implement any/all contact filtering**

```python
def _matches_contacts(lead, required_contacts, match):
    values = [bool(lead.get(field)) for field in required_contacts]
    return all(values) if match == "all" else any(values)
```

Use it for both web evidence and any retained compatibility path.

- [ ] **Step 5: Run API tests and commit**

Commit: `feat: create tasks from natural language`.

### Task 4: Hidden Public-Web Pagination and Evidence Threshold

**Files:**
- Modify: `backend/app/collection/providers.py`
- Modify: `backend/app/collection/web_search.py`
- Modify: `backend/app/collection/service.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/collection/test_web_search_provider.py`
- Test: `backend/tests/api/test_collection_mvp.py`

**Interfaces:**
- Produces: `WebSearchProvider.collect(city: str, keyword: str, request_text: str, limit: int | None = None) -> list[dict[str, Any]]` behavior through the existing provider protocol.
- Consumes: parsed `limit_per_city` and fixed confidence threshold 50.

- [ ] **Step 1: Add failing pagination and threshold tests**

Test that Bing page offsets advance, duplicate contacts do not count as new leads, two consecutive no-new pages stop collection, explicit limit stops early, and source evidence with score 50 survives.

- [ ] **Step 2: Run tests and verify RED**

Expected failures: only one result page is requested and score 50 is filtered.

- [ ] **Step 3: Make public web the only built provider**

`build_providers()` returns only `web_search`. Remove AMap from `KNOWN_PROVIDERS`, provider API output, frontend types, and runtime task creation. Leave historical task JSON readable.

- [ ] **Step 4: Implement paginated collection**

Use Bing structured results as the primary source and Sogou only as fallback. Advance Bing's `first` parameter by page size. Stop when explicit limit is met or two pages add no qualified unique contact. Search each city-keyword query independently and union the results.

- [ ] **Step 5: Apply confidence threshold 50**

Web evidence starts at 50 only when company name, selected contact evidence, source URL, city, and keyword core are present. Successful model relevance scores can raise confidence; model failure retains source score.

- [ ] **Step 6: Run provider/API tests and commit**

Commit: `feat: expand hidden public-web collection`.

### Task 5: Company Name, Service Intro, and Timestamp Cleaning

**Files:**
- Modify: `backend/app/collection/web_search.py`
- Modify: `backend/app/collection/normalization.py`
- Test: `backend/tests/collection/test_web_search_provider.py`
- Test: `backend/tests/api/test_collection_mvp.py`

**Interfaces:**
- Produces: `clean_company_name(title: str, text: str) -> str`.
- Produces: `clean_service_intro(text: str, contacts: Iterable[str]) -> str`.
- Produces: `china_saved_at() -> str`.

- [ ] **Step 1: Add failing regression tests from the screenshots**

```python
assert clean_company_name("窗帘_墙纸_灯饰厂家-武汉宝蓝软装设计公司", "") == "武汉宝蓝软装设计公司"
assert clean_service_intro("联系人：邹，电话：13800138000 武汉宝蓝软装设计公司主营软装设计", ["13800138000"]) == "主营软装设计"
assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", china_saved_at())
```

- [ ] **Step 2: Run tests and verify RED**

Expected: noisy title and contact text remain; timestamp is ISO 8601.

- [ ] **Step 3: Implement deterministic cleaning**

Company names must contain a plausible organization suffix (`公司`, `有限公司`, `工作室`, `设计院`, `事务所`, `中心`) or be rejected. Remove title prefixes/suffixes and product-category fragments outside the organization span.

Service intro must strip phones, emails, WeChat labels, contact-person clauses, addresses, navigation text, and repeated company name. Normalize whitespace and punctuation; return empty string when no business statement remains.

Use `datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")`.

- [ ] **Step 4: Run cleaning and export tests**

Verify XLSX/CSV/JSON contain the formatted local timestamp and cleaned fields.

- [ ] **Step 5: Commit**

Commit: `fix: clean public lead identity and descriptions`.

### Task 6: One-Box React Experience

**Files:**
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/styles.css`

**Interfaces:**
- Consumes: `POST /api/v1/collection-tasks/from-natural-language`.
- Displays: parsed task plan, progress, results, exports, and delivery.

- [ ] **Step 1: Replace the task form**

Keep one textarea labelled `用一句话说明要找什么客户` and one `开始采集` button. Remove visible city, keyword, contact, model, provider, AMap, and web-search controls.

- [ ] **Step 2: Add parsed-plan visibility**

After creation, show compact read-only values for city scope, keywords, model, contact rule, and explicit/default quantity. Do not require confirmation.

- [ ] **Step 3: Wire the new endpoint**

```typescript
createNaturalLanguageTask: (requestText: string) => request<CollectionTask>(
  '/collection-tasks/from-natural-language',
  { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ request_text: requestText }) },
)
```

- [ ] **Step 4: Preserve result/export/delivery workflows**

Keep city filtering, confidence labels, source link, selectable export format, ZIP download, push API, retry, and progress polling.

- [ ] **Step 5: Build and commit**

Run: `conda run -n doubao-leads --no-capture-output npm.cmd run build`

Commit: `feat: simplify lead collection to one request`.

### Task 7: End-to-End Verification and Delivery

**Files:**
- Modify only files required by failures found during verification.

**Interfaces:**
- Verifies all interfaces produced by Tasks 1-6.

- [ ] **Step 1: Run backend verification**

Run focused collection tests, then Ruff. Run the full suite with a bounded timeout and report any environment-only blocker separately.

- [ ] **Step 2: Run frontend build**

Require TypeScript and Vite build exit code 0.

- [ ] **Step 3: Restart services**

Start FastAPI on `127.0.0.1:8010` from the project root and Vite on `127.0.0.1:5173`. Verify page, providers/models proxy, and health endpoints all return 200.

- [ ] **Step 4: Run real sample tasks**

Run one explicit five-city request and one default request limited to one city for cost control. Confirm nonzero evidence-backed leads, valid company names, cleaned intros, formatted timestamps, source URLs, deduplication, and confidence at least 50 without printing contacts.

- [ ] **Step 5: Commit and push**

Commit any final fixes and push `codex/five-city-lead-platform` to Gitee.
