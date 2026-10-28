# 豆包专用浏览器长期登录 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让采集系统复用一个由用户持有、长期保持登录的专用 Chrome，并在后端重启或 CDP 断线后恢复连接而不关闭浏览器或丢失采集进度。

**Architecture:** 新增只负责启动和探测本机专用 Chrome 的组件，使用固定独立用户目录与仅监听 `127.0.0.1` 的 CDP 端口。Playwright 改为通过 `connect_over_cdp` 附着现有 Chrome，网关关闭只断开客户端；API 和前端分别暴露启动、状态与重新连接操作。

**Tech Stack:** Python 3.12、FastAPI、Playwright sync API、pytest、React 19、TypeScript、Vite、Chrome DevTools Protocol

## Global Constraints

- 专用 Chrome 的用户数据目录固定为 `doubao_web_profile_dir`，不得读取、导出、记录 Cookie、密码或会话令牌。
- CDP 地址固定为 `http://127.0.0.1:<port>`，不得监听局域网或公网地址。
- 后端关闭、任务暂停、任务结束及重连时不得关闭 Chrome 进程、远端 browser context 或用户页面。
- 页面关闭时只允许在同一个专用 Chrome 中重建豆包页面；Chrome 被用户关闭后只进入等待状态，不循环弹新窗口。
- 登录失效或安全验证时暂停提交，保留候选队列、去重台账、检查点和已有结果。
- ERP 自动推送配置和默认关闭行为不得被本功能修改。
- 所有文档使用 UTF-8；项目命令使用工作树中的 Python 3.12 `.venv` 与现有 Node 环境。

---

### Task 1: 专用 Chrome 启动器

**Files:**
- Create: `backend/app/collection/dedicated_chrome.py`
- Create: `backend/tests/collection/test_dedicated_chrome.py`
- Modify: `backend/app/config.py:28-31`
- Modify: `.env.example`

**Interfaces:**
- Consumes: `Path` 用户目录、`chat_url: str`、`cdp_host: str = "127.0.0.1"`、`cdp_port: int = 9223`。
- Produces: `DedicatedChromeLauncher(profile_dir, chat_url, cdp_host, cdp_port)`、`cdp_url: str`、`is_running() -> bool`、`start() -> bool`。`start()` 返回 `True` 表示本次启动新进程，返回 `False` 表示复用已有进程。
- Produces settings: `doubao_web_cdp_host: str`、`doubao_web_cdp_port: int`、`doubao_web_browser_executable: Path | None`。

- [ ] **Step 1: Write the failing launcher tests**

```python
def test_start_reuses_existing_local_cdp_without_spawning(tmp_path, monkeypatch):
    launcher = DedicatedChromeLauncher(tmp_path / "profile", "https://www.doubao.com/chat/")
    monkeypatch.setattr(launcher, "is_running", lambda: True)
    spawned = []
    monkeypatch.setattr(launcher, "_spawn", lambda command: spawned.append(command))
    assert launcher.start() is False
    assert spawned == []


def test_start_uses_fixed_profile_and_loopback_cdp(tmp_path, monkeypatch):
    launcher = DedicatedChromeLauncher(
        tmp_path / "profile",
        "https://www.doubao.com/chat/",
        executable=Path("C:/Chrome/chrome.exe"),
        cdp_port=9333,
    )
    checks = iter([False, True])
    monkeypatch.setattr(launcher, "is_running", lambda: next(checks))
    commands = []
    monkeypatch.setattr(launcher, "_spawn", lambda command: commands.append(command))
    assert launcher.start() is True
    assert "--remote-debugging-address=127.0.0.1" in commands[0]
    assert "--remote-debugging-port=9333" in commands[0]
    assert f"--user-data-dir={tmp_path / 'profile'}" in commands[0]
```

- [ ] **Step 2: Run tests to verify RED**

Run: `backend/../.venv/Scripts/python.exe -m pytest backend/tests/collection/test_dedicated_chrome.py -q`

Expected: FAIL during import because `app.collection.dedicated_chrome` does not exist.

- [ ] **Step 3: Implement the minimal launcher and settings**

```python
class DedicatedChromeLauncher:
    def __init__(self, profile_dir: Path, chat_url: str, *, cdp_host: str = "127.0.0.1", cdp_port: int = 9223, executable: Path | None = None) -> None: ...

    @property
    def cdp_url(self) -> str:
        return f"http://{self.cdp_host}:{self.cdp_port}"

    def is_running(self) -> bool:
        with urlopen(f"{self.cdp_url}/json/version", timeout=1) as response:
            return response.status == 200

    def start(self) -> bool:
        if self.is_running():
            return False
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._spawn([
            str(self.executable or installed_browser_executable()),
            f"--remote-debugging-address={self.cdp_host}",
            f"--remote-debugging-port={self.cdp_port}",
            f"--user-data-dir={self.profile_dir}",
            "--no-first-run",
            self.chat_url,
        ])
        self._wait_until_running()
        return True
```

Use `subprocess.Popen(..., creationflags=CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS)` without retaining a process handle and do not add any terminate/kill method. Reject any `cdp_host` other than `127.0.0.1`.

- [ ] **Step 4: Run focused tests to verify GREEN**

Run: `.venv/Scripts/python.exe -m pytest backend/tests/collection/test_dedicated_chrome.py -q`

Expected: PASS, including reuse, loopback validation, missing executable and startup timeout cases.

- [ ] **Step 5: Commit only Task 1 files**

```bash
git add backend/app/collection/dedicated_chrome.py backend/tests/collection/test_dedicated_chrome.py backend/app/config.py .env.example
git commit -m "feat: add dedicated doubao chrome launcher"
```

---

### Task 2: CDP Adapter And Gateway Reconnection

**Files:**
- Modify: `backend/app/collection/doubao_browser.py:45-341`
- Modify: `backend/tests/collection/test_doubao_browser.py:16-197`

**Interfaces:**
- Consumes: `BrowserAdapter.start(*, cdp_url: str, chat_url: str) -> None`.
- Produces: `PlaywrightDoubaoAdapter` that calls `chromium.connect_over_cdp(cdp_url)`, selects an existing `doubao.com/chat` page or creates one in the same remote context, and whose `close()` only calls `playwright.stop()` to disconnect the Playwright client; it must never call `browser.close()`、`context.close()` or page close.
- Produces: `DoubaoWebChatGateway(cdp_url, chat_url, request_timeout_seconds, adapter_factory)` with `start()`, `status()`, `refresh_status()`, `reconnect()`, `discover_companies()`, `enrich_company()`, and `shutdown()`.
- Produces states: `browser_stopped`, `connecting`, `waiting_for_login`, `ready`, `working`, `reconnecting`, `timeout`, `unavailable`.

- [ ] **Step 1: Write failing CDP ownership and reconnect tests**

```python
def test_adapter_connects_to_existing_browser_and_reuses_doubao_page():
    adapter = PlaywrightDoubaoAdapter()
    adapter._playwright = fake_playwright_with_pages([
        FakePage("https://example.test/"),
        FakePage("https://www.doubao.com/chat/123"),
    ])
    adapter._connect("http://127.0.0.1:9223", "https://www.doubao.com/chat/")
    assert adapter._page.url.endswith("/chat/123")
    assert adapter._context.close_calls == 0


def test_adapter_close_disconnects_client_without_closing_remote_context():
    adapter = PlaywrightDoubaoAdapter()
    adapter._browser = RecordingBrowser()
    adapter._context = RecordingContext()
    adapter._playwright = RecordingPlaywright()
    adapter.close()
    assert adapter._browser.close_calls == 0
    assert adapter._context.close_calls == 0
    assert adapter._playwright.stopped is True


def test_gateway_reconnects_after_cdp_disconnect_without_losing_command(tmp_path):
    disconnected = ClosedPageAdapter([])
    recovered = FakeAdapter(["1｜深圳市测试设计有限公司｜https://example.cn/about｜主营别墅全案"])
    adapters = iter([disconnected, recovered])
    gateway = DoubaoWebChatGateway(cdp_url="http://127.0.0.1:9223", adapter_factory=lambda: next(adapters))
    gateway.start()
    result = gateway.discover_companies(city="深圳市", keywords=["别墅设计"], request_text="查找别墅全案公司")
    assert result[0].company_name == "深圳市测试设计有限公司"
    assert disconnected.closed is True
    assert gateway.status().state == "ready"
```

- [ ] **Step 2: Run tests to verify RED**

Run: `.venv/Scripts/python.exe -m pytest backend/tests/collection/test_doubao_browser.py -q`

Expected: FAIL because the gateway still requires `profile_dir`, adapter launches a persistent context, and `reconnect()` does not exist.

- [ ] **Step 3: Implement CDP attachment and bounded reconnection**

```python
class BrowserAdapter(Protocol):
    def start(self, *, cdp_url: str, chat_url: str) -> None: ...


class PlaywrightDoubaoAdapter:
    def start(self, *, cdp_url: str, chat_url: str) -> None:
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.connect_over_cdp(cdp_url)
        self._context = self._browser.contexts[0]
        self._page = next(
            (page for page in self._context.pages if "doubao.com/chat" in page.url),
            None,
        ) or self._context.new_page()
        if "doubao.com/chat" not in self._page.url:
            self._page.goto(chat_url, wait_until="domcontentloaded")

    def close(self) -> None:
        if self._playwright is not None:
            self._playwright.stop()
```

In the gateway actor, set `connecting` before the first attachment and `reconnecting` before retry. Retry three times with delays `0.25`, `0.5`, `1.0` seconds. A failed reconnect must return `browser_stopped` when CDP is unavailable, preserve the command queue, and never call the launcher or create a new OS process.

- [ ] **Step 4: Run focused and service regression tests**

Run: `.venv/Scripts/python.exe -m pytest backend/tests/collection/test_doubao_browser.py backend/tests/collection/test_continuous_collection.py backend/tests/collection/test_two_stage_service.py -q`

Expected: PASS; existing login/challenge behavior and continuous task checkpoints remain intact.

- [ ] **Step 5: Commit only Task 2 files**

```bash
git add backend/app/collection/doubao_browser.py backend/tests/collection/test_doubao_browser.py
git commit -m "feat: attach doubao gateway to persistent chrome"
```

---

### Task 3: Backend Wiring And Browser Control API

**Files:**
- Modify: `backend/app/main.py:9-70`
- Modify: `backend/app/api/routes/collection_tasks.py:192-219`
- Modify: `backend/tests/api/test_collection_mvp.py:156-309`

**Interfaces:**
- Consumes: `DedicatedChromeLauncher.start()`, `.is_running()`, `.cdp_url`; `DoubaoWebChatGateway.reconnect()`.
- Produces app state: `app.state.doubao_web_launcher`.
- Produces routes: `GET /api/v1/collection-tasks/doubao-web/status`, `POST /api/v1/collection-tasks/doubao-web/start`, `POST /api/v1/collection-tasks/doubao-web/reconnect`.
- All route responses retain the sanitized shape `{enabled, state, detail, current_company}`.

- [ ] **Step 1: Write failing API tests**

```python
def test_doubao_start_launches_dedicated_chrome_then_connects(collection_client):
    launcher = FakeDedicatedChromeLauncher(running=False)
    gateway = FakeDoubaoWebGateway()
    collection_client.app.state.doubao_web_launcher = launcher
    collection_client.app.state.collection_service.doubao_web_gateway = gateway
    response = collection_client.post("/api/v1/collection-tasks/doubao-web/start")
    assert response.status_code == 200
    assert launcher.start_calls == 1
    assert gateway.start_calls == 1


def test_doubao_reconnect_does_not_launch_or_close_chrome(collection_client):
    launcher = FakeDedicatedChromeLauncher(running=True)
    gateway = FakeDoubaoWebGateway()
    collection_client.app.state.doubao_web_launcher = launcher
    collection_client.app.state.collection_service.doubao_web_gateway = gateway
    response = collection_client.post("/api/v1/collection-tasks/doubao-web/reconnect")
    assert response.status_code == 200
    assert launcher.start_calls == 0
    assert gateway.reconnect_calls == 1
```

- [ ] **Step 2: Run API tests to verify RED**

Run: `.venv/Scripts/python.exe -m pytest backend/tests/api/test_collection_mvp.py -k "doubao_web" -q`

Expected: FAIL because launcher app state and reconnect route are absent.

- [ ] **Step 3: Wire launcher, gateway, and routes**

```python
launcher = DedicatedChromeLauncher(
    profile_dir,
    resolved_settings.doubao_web_chat_url,
    cdp_host=resolved_settings.doubao_web_cdp_host,
    cdp_port=resolved_settings.doubao_web_cdp_port,
    executable=resolved_settings.doubao_web_browser_executable,
)
gateway = DoubaoWebChatGateway(
    cdp_url=launcher.cdp_url,
    chat_url=resolved_settings.doubao_web_chat_url,
    request_timeout_seconds=resolved_settings.doubao_web_request_timeout_seconds,
)
```

`start` must call `launcher.start()` before `gateway.start()`. `status` must return `browser_stopped` without trying to launch when `launcher.is_running()` is false. `reconnect` must call only `gateway.reconnect()` and return 409 when Chrome is not running.

- [ ] **Step 4: Run API and backend regression tests**

Run: `.venv/Scripts/python.exe -m pytest backend/tests/api/test_collection_mvp.py backend/tests/collection/test_doubao_browser.py backend/tests/collection/test_dedicated_chrome.py -q`

Expected: PASS and no response contains cookies, tokens, passwords or profile contents.

- [ ] **Step 5: Commit only Task 3 files**

```bash
git add backend/app/main.py backend/app/api/routes/collection_tasks.py backend/tests/api/test_collection_mvp.py
git commit -m "feat: expose persistent doubao browser controls"
```

---

### Task 4: Frontend Browser State And Controls

**Files:**
- Modify: `frontend/src/types.ts:7-13`
- Modify: `frontend/src/api.ts:44-46`
- Modify: `frontend/src/App.tsx:1-180`
- Modify: `frontend/src/styles.css`（仅在双按钮布局需要额外样式时）
- Modify: `frontend/src/labels.ts`
- Modify: `frontend/src/labels.test.ts`

**Interfaces:**
- Consumes: the three browser routes and sanitized `DoubaoWebStatus` response.
- Produces: browser state labels for `browser_stopped`, `connecting`, `waiting_for_login`, `ready`, `working`, `reconnecting`, `timeout`, `unavailable`.
- Produces UI commands: `启动专用浏览器` and `重新连接`.

- [ ] **Step 1: Write failing state-label tests**

```typescript
assert.equal(doubaoWebStateLabel('browser_stopped'), '专用浏览器未启动')
assert.equal(doubaoWebStateLabel('connecting'), '正在连接')
assert.equal(doubaoWebStateLabel('reconnecting'), '正在重新连接')
assert.equal(isDoubaoWebReady('working'), true)
assert.equal(isDoubaoWebReady('waiting_for_login'), false)
```

- [ ] **Step 2: Run test/typecheck to verify RED**

Run: `node --test frontend/src/labels.test.ts`

Expected: FAIL because `doubaoWebStateLabel` and the new states do not exist.

- [ ] **Step 3: Implement minimal frontend changes**

```typescript
export type DoubaoWebState = 'disabled' | 'browser_stopped' | 'connecting'
  | 'waiting_for_login' | 'ready' | 'working' | 'reconnecting' | 'timeout' | 'unavailable'

reconnectDoubaoWeb: () => request<DoubaoWebStatus>(
  '/collection-tasks/doubao-web/reconnect',
  { method: 'POST' },
)
```

Replace `打开登录窗口` with `启动专用浏览器`. Show `重新连接` only when the state is `unavailable`, `timeout`, `browser_stopped`, or `reconnecting`; disable it while a request is active. Keep the existing natural-language task form and ERP toggle unchanged.

- [ ] **Step 4: Run frontend verification**

Run: `node --test frontend/src/labels.test.ts`

Expected: PASS.

Run: `npm.cmd run typecheck --prefix frontend`

Expected: PASS with no TypeScript errors.

Run: `npm.cmd run build --prefix frontend`

Expected: PASS and Vite emits production assets.

- [ ] **Step 5: Commit only Task 4 files**

```bash
git add frontend/src/types.ts frontend/src/api.ts frontend/src/App.tsx frontend/src/styles.css frontend/src/labels.ts frontend/src/labels.test.ts
git commit -m "feat: show dedicated doubao browser controls"
```

---

### Task 5: End-To-End Verification And Operating Check

**Files:**
- Modify only if verification exposes a defect: files already listed in Tasks 1-4, with a new failing regression test before each fix.

**Interfaces:**
- Consumes: dedicated Chrome launcher, CDP gateway, browser APIs, frontend controls.
- Produces: evidence that backend lifecycle does not own Chrome lifecycle and existing lead collection remains compatible.

- [ ] **Step 1: Run the full focused backend suite**

Run: `.venv/Scripts/python.exe -m pytest backend/tests/collection/test_dedicated_chrome.py backend/tests/collection/test_doubao_browser.py backend/tests/collection/test_continuous_collection.py backend/tests/collection/test_two_stage_service.py backend/tests/api/test_collection_mvp.py -q`

Expected: PASS with zero failures.

- [ ] **Step 2: Run lint and frontend checks**

Run: `.venv/Scripts/python.exe -m ruff check backend/app/collection/dedicated_chrome.py backend/app/collection/doubao_browser.py backend/app/config.py backend/app/main.py backend/app/api/routes/collection_tasks.py backend/tests/collection/test_dedicated_chrome.py backend/tests/collection/test_doubao_browser.py backend/tests/api/test_collection_mvp.py`

Expected: PASS with `All checks passed!`.

Run: `npm.cmd run build --prefix frontend`

Expected: PASS.

- [ ] **Step 3: Perform manual lifecycle acceptance**

1. Click `启动专用浏览器` and log into Doubao once.
2. Verify status becomes `ready` and a Doubao tab exists in the dedicated Chrome.
3. Stop and restart only the backend process.
4. Verify the dedicated Chrome window remains open and logged in.
5. Click `重新连接`; verify status returns to `ready` without a new Chrome process.
6. Start or resume the Shenzhen collection task; verify each accepted lead appears in `采集结果`.
7. Verify ERP auto-push remains off unless the existing switch is explicitly enabled.

Expected: all seven checks pass; no login data is printed in backend logs.

- [ ] **Step 4: Refresh the existing 31-lead manual task display**

Run: `.venv/Scripts/python.exe -X utf8 .planning/import_manual_24_results.py`

Expected: JSON contains `"task_id":"a18629bc-8603-4862-93f2-3b1939f4f52d"` and `"lead_count":31`; refreshing `http://127.0.0.1:5173/` displays 31 rows for that task.

- [ ] **Step 5: Review the final diff without staging unrelated dirty files**

Run: `git diff -- backend/app/collection/dedicated_chrome.py backend/app/collection/doubao_browser.py backend/app/config.py backend/app/main.py backend/app/api/routes/collection_tasks.py backend/tests/collection/test_dedicated_chrome.py backend/tests/collection/test_doubao_browser.py backend/tests/api/test_collection_mvp.py frontend/src/types.ts frontend/src/api.ts frontend/src/App.tsx frontend/src/styles.css frontend/src/labels.ts frontend/src/labels.test.ts .env.example`

Expected: only persistent-browser changes and pre-existing overlapping edits are present; no secrets, Cookie values, token values, generated test caches or unrelated files are staged.
