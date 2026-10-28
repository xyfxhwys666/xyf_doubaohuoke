# 公司名称公开证据门槛实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在模型发现候选与豆包联系方式补全之间增加公开网页公司名称核验，阻止无证据候选调用豆包或自动推送 ERP。

**Architecture:** `WebSearchProvider` 负责返回结构化名称证据，`CollectionService` 负责逐条核验、持久化和流程门控，`continuous.py` 负责最终 ERP 准入。任务 JSON 是当前运行时事实来源；SQLAlchemy 模型同步增加字段以保持未来 PostgreSQL 存储契约一致。

**Tech Stack:** Python 3.12、FastAPI、httpx、pytest、React、TypeScript、Vite。

## Global Constraints

- 联系方式仍只由豆包逐家公司查询，不回退到 DeepSeek 或其他模型。
- 公司名仅依据公开 HTTP(S) 网页证据核验；本阶段不抓取企查查网页。
- 只有 `verified_public_web` 或 `verified_registry` 才允许进入豆包补全和 ERP 准入。
- 名称核验失败或证据不足的候选保留本地并展示原因。
- 现有模型选择、持续采集、暂停、继续、终止、下载和 ERP 幂等行为不得回归。

---

### Task 1: 公开网页公司名称证据判定

**Files:**
- Modify: `backend/app/collection/web_search.py`
- Test: `backend/tests/collection/test_web_search_provider.py`

**Interfaces:**
- Produces: `CompanyNameEvidence(status, evidence_urls, reason, city_matched, keyword_matched)`。
- Produces: `WebSearchProvider.verify_company_name(*, company_name: str, city: str, keywords: list[str]) -> CompanyNameEvidence`。

- [ ] **Step 1: 写单页和双域名通过、简称/错城市/无效 URL 拒绝的失败测试**

```python
evidence = provider.verify_company_name(
    company_name="深圳市甲软装设计有限公司",
    city="深圳市",
    keywords=["软装公司", "全案设计公司"],
)
assert evidence.status == "verified_public_web"
assert evidence.evidence_urls == ["https://example.test/company"]
```

- [ ] **Step 2: 运行目标测试并确认因接口不存在而失败**

Run: `conda run -n doubao-leads --no-capture-output python -m pytest --basetemp .pytest-company-name-red backend/tests/collection/test_web_search_provider.py -q`

Expected: FAIL，错误指向 `CompanyNameEvidence` 或 `verify_company_name` 尚不存在。

- [ ] **Step 3: 实现最小结构化证据判定**

```python
@dataclass(frozen=True, slots=True)
class CompanyNameEvidence:
    status: str
    evidence_urls: list[str]
    reason: str
    city_matched: bool
    keyword_matched: bool
```

搜索 `城市 + 完整公司名称`，复用 `_get`、`_SearchResultParser` 和 `_visible_text`。只有单页同时命中完整名称、城市和业务词，或两个不同域名共同满足规则时返回 `verified_public_web`；请求全部异常返回 `verification_failed`，其余返回 `unverified` 或 `rejected`。

- [ ] **Step 4: 运行目标测试并确认通过**

Run: `conda run -n doubao-leads --no-capture-output python -m pytest --basetemp .pytest-company-name-green backend/tests/collection/test_web_search_provider.py -q`

Expected: PASS。

- [ ] **Step 5: 提交公开证据判定**

```bash
git add backend/app/collection/web_search.py backend/tests/collection/test_web_search_provider.py
git commit -m "feat: verify company names with public evidence"
```

### Task 2: 名称核验状态持久化与豆包门控

**Files:**
- Modify: `backend/app/collection/normalization.py`
- Modify: `backend/app/collection/service.py`
- Modify: `backend/app/models/collection.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/collection/test_two_stage_service.py`
- Test: `backend/tests/models/test_collection_models.py`

**Interfaces:**
- Consumes: `CompanyNameEvidence` 与 `verify_company_name(...)`。
- Produces: 每条线索的 `company_name_verification_status`、`company_name_evidence_urls`、`company_name_verification_reason`、`company_name_verified_at`、`company_name_verification_attempts`。
- Produces: `CollectionService(..., company_name_verifier=...)`，生产环境由 `create_app` 显式注入公开网页提供器。

- [ ] **Step 1: 写“未通过不调用豆包、通过才调用、异常不中断其他候选、已核验不重复请求”的失败测试**

```python
assert rejected["company_name_verification_status"] == "unverified"
assert rejected["company_name"] not in doubao.enrichment_calls
assert verified["company_name"] in doubao.enrichment_calls
assert company_verifier.call_counts[verified["company_name"]] == 1
```

- [ ] **Step 2: 运行目标测试并确认门控尚未实现**

Run: `conda run -n doubao-leads --no-capture-output python -m pytest --basetemp .pytest-company-gate-red backend/tests/collection/test_two_stage_service.py -q`

Expected: FAIL，未通过名称证据的候选仍调用豆包。

- [ ] **Step 3: 增加默认字段和持久化列**

```python
"company_name_verification_status": "unverified",
"company_name_evidence_urls": [],
"company_name_verification_reason": "",
"company_name_verified_at": None,
"company_name_verification_attempts": 0,
```

SQLAlchemy `CollectionLead` 同步增加字符串、JSON、文本、时间和整数列；任务 JSON 旧数据通过 `_with_task_defaults`/线索默认填充按 `unverified` 读取。

- [ ] **Step 4: 在候选持久化后增加最多三个并发的核验阶段**

`_verify_city_company_names` 只处理未终态候选；以 `(city, normalized_company_name)` 作为任务内缓存键，逐条保存结果并执行暂停/终止检查。`_enrich_city_candidates` 只选择 `verified_public_web` 或 `verified_registry`。

- [ ] **Step 5: 运行服务和模型测试并确认通过**

Run: `conda run -n doubao-leads --no-capture-output python -m pytest --basetemp .pytest-company-gate-green backend/tests/collection/test_two_stage_service.py backend/tests/models/test_collection_models.py -q`

Expected: PASS。

- [ ] **Step 6: 提交流程门控**

```bash
git add backend/app/collection/normalization.py backend/app/collection/service.py backend/app/models/collection.py backend/app/main.py backend/tests/collection/test_two_stage_service.py backend/tests/models/test_collection_models.py
git commit -m "feat: gate doubao enrichment on company evidence"
```

### Task 3: ERP 公司名称安全门槛

**Files:**
- Modify: `backend/app/collection/continuous.py`
- Test: `backend/tests/collection/test_continuous_collection.py`
- Test: `backend/tests/collection/test_two_stage_service.py`

**Interfaces:**
- Consumes: `company_name_verification_status`。
- Produces: `verified_erp_ineligibility` 的 `company_name_unverified` 原因。

- [ ] **Step 1: 写未核验公司即使有手机、来源和模型匹配也不得推送的失败测试**

```python
assert verified_erp_ineligibility({**eligible, "company_name_verification_status": "unverified"}) == "company_name_unverified"
assert verified_erp_ineligibility({**eligible, "company_name_verification_status": "verified_public_web"}) is None
```

- [ ] **Step 2: 运行目标测试并确认旧准入错误放行**

Run: `conda run -n doubao-leads --no-capture-output python -m pytest --basetemp .pytest-company-erp-red backend/tests/collection/test_continuous_collection.py -q`

Expected: FAIL，旧实现返回 `None`。

- [ ] **Step 3: 在手机号检查前增加名称核验状态检查**

```python
if lead.get("company_name_verification_status") not in {
    "verified_public_web", "verified_registry"
}:
    return "company_name_unverified"
```

- [ ] **Step 4: 运行 ERP 准入与两阶段服务测试**

Run: `conda run -n doubao-leads --no-capture-output python -m pytest --basetemp .pytest-company-erp-green backend/tests/collection/test_continuous_collection.py backend/tests/collection/test_two_stage_service.py -q`

Expected: PASS。

- [ ] **Step 5: 提交 ERP 门槛**

```bash
git add backend/app/collection/continuous.py backend/tests/collection/test_continuous_collection.py backend/tests/collection/test_two_stage_service.py
git commit -m "fix: require company evidence for erp delivery"
```

### Task 4: 前端展示名称核验阶段与证据

**Files:**
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/styles.css`

**Interfaces:**
- Consumes: 后端线索名称核验字段。
- Produces: 名称核验中文标签、证据链接和通过/未通过数量。

- [ ] **Step 1: 增加 TypeScript 字段并让构建暴露缺失使用点**

```typescript
company_name_verification_status: 'verified_public_web' | 'verified_registry' | 'unverified' | 'rejected' | 'verification_failed'
company_name_evidence_urls: string[]
company_name_verification_reason: string
company_name_verified_at: string | null
company_name_verification_attempts: number
```

- [ ] **Step 2: 增加结果列和进度摘要**

表格新增“公司名称核验”列，显示中文状态；存在证据 URL 时显示可打开的“证据”链接。进度摘要显示名称通过和未通过数量，不新增开关。

- [ ] **Step 3: 运行生产构建**

Run: `conda run -n doubao-leads --no-capture-output npm.cmd --prefix frontend run build`

Expected: `vite build` exit 0。

- [ ] **Step 4: 提交前端展示**

```bash
git add frontend/src/types.ts frontend/src/App.tsx frontend/src/styles.css
git commit -m "feat: show company name evidence status"
```

### Task 5: 全量验证、浏览器检查、服务重启与推送

**Files:**
- Verify: `backend/tests`
- Verify: `frontend`

**Interfaces:**
- Consumes: Tasks 1-4 的完整链路。
- Produces: 可运行的本地服务和已推送的 Gitee 分支。

- [ ] **Step 1: 运行后端全量测试和 Ruff**

Run: `conda run -n doubao-leads --no-capture-output python -m pytest --basetemp .pytest-company-final backend/tests -q`

Expected: 全部 PASS。

Run: `conda run -n doubao-leads --no-capture-output python -m ruff check backend`

Expected: `All checks passed!`。

- [ ] **Step 2: 运行前端生产构建**

Run: `conda run -n doubao-leads --no-capture-output npm.cmd --prefix frontend run build`

Expected: exit 0。

- [ ] **Step 3: 重启本地服务并验证健康检查**

Run: `conda run -n doubao-leads --no-capture-output python .planning/restart_local_services.py`

Expected: `http://127.0.0.1:8010/api/v1/health` 返回 `{"status":"ok"}`，前端 `http://127.0.0.1:5173/` 可打开。

- [ ] **Step 4: 用浏览器验证桌面与 375px 移动端**

确认模型选择区未变化、名称核验列可读、证据链接可打开、无横向页面溢出；表格本身允许水平滚动。

- [ ] **Step 5: 提交必要修正并推送 Gitee**

```bash
git push origin codex/five-city-lead-platform
```

Expected: 远程分支更新成功，且 `.env`、`.planning`、pytest 缓存和日志均未提交。
