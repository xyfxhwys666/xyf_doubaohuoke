# Doubao Compatible Proxy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让豆包配置同时支持火山方舟 `ep-` 接入点和第三方 OpenAI 兼容接口的直接模型名称。

**Architecture:** 根据 `DOUBAO_BASE_URL` 判断是否为火山方舟。方舟保持 `ep-` 限制，其他兼容地址不设置模型前缀限制；运行时仍复用现有 `OpenAICompatibleVerifier`。

**Tech Stack:** Python 3.12、pydantic-settings、pytest

## Global Constraints

- 火山方舟地址必须使用 `ep-` 接入点。
- `api.kuai.host` 使用 `doubao-seed-2-0-lite-260428`。
- API Key 保持空白并由用户手动填写。
- `.env` 不进入 Git 提交。

---

### Task 1: Support Direct Model Names For Compatible Proxies

**Files:**
- Modify: `backend/app/collection/models.py`
- Test: `backend/tests/collection/test_model_verification.py`
- Modify locally: `.env`

**Interfaces:**
- Consumes: `Settings.doubao_base_url` and `Settings.doubao_model`.
- Produces: `build_model_verifiers(settings)` with conditional `required_model_prefix`.

- [ ] **Step 1: Write the failing test**

```python
def test_doubao_compatible_proxy_accepts_direct_model_name() -> None:
    settings = Settings(
        app_env="test",
        database_url="sqlite+pysqlite:///:memory:",
        redis_url="redis://localhost:6379/15",
        app_master_key=Fernet.generate_key().decode("ascii"),
        session_secret=token_urlsafe(32),
        doubao_api_key="secret",
        doubao_base_url="https://api.kuai.host/v1",
        doubao_model="doubao-seed-2-0-lite-260428",
    )
    assert build_model_verifiers(settings)["doubao"].configured is True
```

- [ ] **Step 2: Run the test and verify RED**

Run: `conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/collection/test_model_verification.py -q`

Expected: FAIL because the current builder always requires `ep-`.

- [ ] **Step 3: Implement conditional prefix validation**

```python
required_model_prefix=(
    "ep-" if "volces.com" in (settings.doubao_base_url or "").lower() else None
)
```

- [ ] **Step 4: Run tests and lint**

Run: `conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/collection/test_model_verification.py -q`

Expected: all tests pass.

Run: `conda run -n doubao-leads --no-capture-output python -m ruff check backend`

Expected: all checks pass.

- [ ] **Step 5: Update local configuration and open it**

Set `DOUBAO_API_KEY` to empty, `DOUBAO_BASE_URL` to `https://api.kuai.host/v1`, and `DOUBAO_MODEL` to `doubao-seed-2-0-lite-260428`. Open the root `.env` in Notepad for the user to fill the key.
