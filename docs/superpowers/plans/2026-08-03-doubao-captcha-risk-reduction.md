# Doubao CAPTCHA Risk Reduction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce Doubao web-chat challenge frequency with persistent conservative pacing, one active web task per account, and automatic checkpoint resume after a user manually completes a challenge.

**Architecture:** Add three focused backend components: a JSON-backed prompt pacer, an in-process task lease, and a read-only attention monitor. `DoubaoWebChatGateway` owns pacing, `CollectionService` owns the account lease, and application lifespan owns the monitor. Existing task checkpoints, persistent Chrome profile, ERP delivery state, and deduplication remain unchanged.

**Tech Stack:** Python 3.12, FastAPI, Playwright CDP, Pydantic Settings, pytest, React 19, TypeScript, Node.js 22.

## Global Constraints

- Do not recognize, solve, click, or bypass CAPTCHA challenges.
- Do not forge mouse movement, browser fingerprints, cookies, or account behavior.
- Reuse the existing persistent Chrome profile and CDP port; do not create another browser profile or Doubao tab.
- Production prompt intervals must be randomized between 15 and 30 seconds.
- Production batch size must be randomized between 20 and 25 prompts.
- Production batch cooldown must be randomized between 1200 and 1800 seconds.
- A Doubao account may have only one owning web-chat task at a time.
- A challenge keeps the account lease with the interrupted task until manual verification succeeds or the task is terminated.
- Page readiness checks occur every 60 seconds, submit no prompt, and require two consecutive `ready` results before automatic resume.
- Existing collection results, checkpoints, ERP states, and deduplication ledgers must survive restarts and remain backward compatible.
- Run Python and Node commands through the `doubao-leads` conda environment.

---

## File Map

- Create `backend/app/collection/prompt_pacing.py`: persistent prompt timing and batch state.
- Create `backend/app/collection/doubao_task_lease.py`: one-owner account lease with cooperative waiting.
- Create `backend/app/collection/doubao_attention.py`: low-frequency readiness monitor and automatic resume callback.
- Modify `backend/app/collection/doubao_browser.py`: delegate prompt timing to the persistent pacer.
- Modify `backend/app/collection/service.py`: acquire, retain, and release the account lease around web tasks.
- Modify `backend/app/config.py`: conservative production defaults and monitor settings.
- Modify `backend/app/main.py`: construct pacing state and start/stop the attention monitor.
- Modify `.env.example`: document all non-secret controls.
- Modify `frontend/src/labels.ts` and `frontend/src/App.tsx`: explain automatic resume after manual verification.
- Add focused backend tests and update frontend label tests.

---

### Task 1: Persistent Conservative Prompt Pacing

**Files:**
- Create: `backend/app/collection/prompt_pacing.py`
- Create: `backend/tests/collection/test_prompt_pacing.py`
- Modify: `backend/app/collection/doubao_browser.py:92-119, 364-398`
- Modify: `backend/app/config.py:30-42`
- Modify: `backend/app/main.py:68-81`
- Modify: `backend/tests/collection/test_doubao_browser.py:384-415`
- Modify: `backend/tests/test_config.py:8-25`
- Modify: `.env.example`

**Interfaces:**
- Produces: `PersistentPromptPacer.wait_for_slot() -> None` and `PersistentPromptPacer.mark_submitted() -> None`.
- Consumes: a JSON `state_path`, `now_fn`, `sleep_fn`, `uniform_fn`, and `randint_fn` so tests run without real delays.
- `DoubaoWebChatGateway` accepts `pacing_state_path`, `batch_size_min`, and `batch_size_max`; the existing `batch_size` argument remains as a fixed-size compatibility override for current tests.

- [ ] **Step 1: Write failing persistence and timing tests**

```python
from pathlib import Path

from app.collection.prompt_pacing import PersistentPromptPacer


class FakeClock:
    def __init__(self) -> None:
        self.value = 1_000.0
        self.delays: list[float] = []

    def now(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.delays.append(seconds)
        self.value += seconds


def test_prompt_pacing_survives_gateway_restart(tmp_path: Path) -> None:
    state_path = tmp_path / "doubao-pacing.json"
    clock = FakeClock()
    first = PersistentPromptPacer(
        state_path=state_path,
        interval_min_seconds=15,
        interval_max_seconds=15,
        batch_size_min=2,
        batch_size_max=2,
        cooldown_min_seconds=1200,
        cooldown_max_seconds=1200,
        now_fn=clock.now,
        sleep_fn=clock.sleep,
        uniform_fn=lambda low, high: low,
        randint_fn=lambda low, high: low,
    )
    first.wait_for_slot()
    first.mark_submitted()

    restarted = PersistentPromptPacer(
        state_path=state_path,
        interval_min_seconds=15,
        interval_max_seconds=15,
        batch_size_min=2,
        batch_size_max=2,
        cooldown_min_seconds=1200,
        cooldown_max_seconds=1200,
        now_fn=clock.now,
        sleep_fn=clock.sleep,
        uniform_fn=lambda low, high: low,
        randint_fn=lambda low, high: low,
    )
    restarted.wait_for_slot()

    assert clock.delays == [15]


def test_prompt_pacing_uses_persisted_batch_cooldown(tmp_path: Path) -> None:
    state_path = tmp_path / "doubao-pacing.json"
    clock = FakeClock()
    pacer = PersistentPromptPacer(
        state_path=state_path,
        interval_min_seconds=15,
        interval_max_seconds=15,
        batch_size_min=2,
        batch_size_max=2,
        cooldown_min_seconds=1200,
        cooldown_max_seconds=1200,
        now_fn=clock.now,
        sleep_fn=clock.sleep,
        uniform_fn=lambda low, high: low,
        randint_fn=lambda low, high: low,
    )
    for _ in range(2):
        pacer.wait_for_slot()
        pacer.mark_submitted()

    restarted = PersistentPromptPacer(
        state_path=state_path,
        interval_min_seconds=15,
        interval_max_seconds=15,
        batch_size_min=2,
        batch_size_max=2,
        cooldown_min_seconds=1200,
        cooldown_max_seconds=1200,
        now_fn=clock.now,
        sleep_fn=clock.sleep,
        uniform_fn=lambda low, high: low,
        randint_fn=lambda low, high: low,
    )
    restarted.wait_for_slot()

    assert clock.delays == [15, 1200]
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/collection/test_prompt_pacing.py -q
```

Expected: collection fails with `ModuleNotFoundError: No module named 'app.collection.prompt_pacing'`.

- [ ] **Step 3: Implement the persistent pacer**

Create `backend/app/collection/prompt_pacing.py` with this public shape and behavior:

```python
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from random import randint, uniform
from time import sleep, time


@dataclass(slots=True)
class PromptPacingState:
    batch_count: int
    batch_target: int
    next_prompt_at: float | None = None
    cooldown_until: float | None = None


class PersistentPromptPacer:
    def __init__(
        self,
        *,
        state_path: Path | None,
        interval_min_seconds: float,
        interval_max_seconds: float,
        batch_size_min: int,
        batch_size_max: int,
        cooldown_min_seconds: float,
        cooldown_max_seconds: float,
        now_fn: Callable[[], float] = time,
        sleep_fn: Callable[[float], None] = sleep,
        uniform_fn: Callable[[float, float], float] = uniform,
        randint_fn: Callable[[int, int], int] = randint,
    ) -> None:
        self.state_path = state_path
        self.interval_min_seconds = max(0.0, interval_min_seconds)
        self.interval_max_seconds = max(self.interval_min_seconds, interval_max_seconds)
        self.batch_size_min = max(1, batch_size_min)
        self.batch_size_max = max(self.batch_size_min, batch_size_max)
        self.cooldown_min_seconds = max(0.0, cooldown_min_seconds)
        self.cooldown_max_seconds = max(self.cooldown_min_seconds, cooldown_max_seconds)
        self.now_fn = now_fn
        self.sleep_fn = sleep_fn
        self.uniform_fn = uniform_fn
        self.randint_fn = randint_fn
        self.state = self._load()

    def wait_for_slot(self) -> None:
        target = max(
            self.state.next_prompt_at or 0.0,
            self.state.cooldown_until or 0.0,
        )
        remaining = target - self.now_fn()
        if remaining > 0:
            self.sleep_fn(remaining)
        if self.state.cooldown_until is not None and self.now_fn() >= self.state.cooldown_until:
            self.state = PromptPacingState(
                batch_count=0,
                batch_target=self.randint_fn(self.batch_size_min, self.batch_size_max),
            )
            self._save()

    def mark_submitted(self) -> None:
        now = self.now_fn()
        self.state.batch_count += 1
        if self.state.batch_count >= self.state.batch_target:
            self.state.next_prompt_at = None
            self.state.cooldown_until = now + self.uniform_fn(
                self.cooldown_min_seconds,
                self.cooldown_max_seconds,
            )
        else:
            self.state.next_prompt_at = now + self.uniform_fn(
                self.interval_min_seconds,
                self.interval_max_seconds,
            )
            self.state.cooldown_until = None
        self._save()

    def _load(self) -> PromptPacingState:
        if self.state_path is not None:
            try:
                raw = json.loads(self.state_path.read_text(encoding="utf-8"))
                return PromptPacingState(
                    batch_count=max(0, int(raw["batch_count"])),
                    batch_target=max(1, int(raw["batch_target"])),
                    next_prompt_at=float(raw["next_prompt_at"]) if raw.get("next_prompt_at") is not None else None,
                    cooldown_until=float(raw["cooldown_until"]) if raw.get("cooldown_until") is not None else None,
                )
            except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                pass
        return PromptPacingState(
            batch_count=0,
            batch_target=self.randint_fn(self.batch_size_min, self.batch_size_max),
        )

    def _save(self) -> None:
        if self.state_path is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(asdict(self.state), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.state_path)
```

In `DoubaoWebChatGateway.__init__`, construct one `PersistentPromptPacer`. Replace `_prompt_count` and `_wait_for_prompt_slot` with:

```python
self._prompt_pacer = PersistentPromptPacer(
    state_path=pacing_state_path,
    interval_min_seconds=request_interval_min_seconds,
    interval_max_seconds=request_interval_max_seconds,
    batch_size_min=batch_size if batch_size is not None else batch_size_min,
    batch_size_max=batch_size if batch_size is not None else batch_size_max,
    cooldown_min_seconds=cooldown_min_seconds,
    cooldown_max_seconds=cooldown_max_seconds,
    sleep_fn=sleep_fn or sleep,
)
```

Immediately before `adapter.submit_prompt(...)`, call:

```python
self._prompt_pacer.wait_for_slot()
self._prompt_pacer.mark_submitted()
```

Do not apply this pacer to `refresh_status()` or `search_public_web()` because those operations submit no Doubao prompt.

- [ ] **Step 4: Wire conservative settings and verify GREEN**

Set these defaults in `Settings`:

```python
doubao_web_min_interval_seconds: float = 15.0
doubao_web_max_interval_seconds: float = 30.0
doubao_web_batch_min_size: int = 20
doubao_web_batch_max_size: int = 25
doubao_web_cooldown_min_seconds: float = 1200.0
doubao_web_cooldown_max_seconds: float = 1800.0
doubao_web_attention_poll_seconds: float = 60.0
doubao_web_ready_confirmations: int = 2
```

Pass `resolved_settings.data_dir / "doubao-web-pacing.json"` and the new batch bounds from `main.py`. Add matching names to `.env.example`. Update `test_config.py` to assert all exact defaults.

Run:

```bash
conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/collection/test_prompt_pacing.py backend/tests/collection/test_doubao_browser.py backend/tests/test_config.py -q
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit persistent pacing**

```bash
git add .env.example backend/app/config.py backend/app/main.py backend/app/collection/prompt_pacing.py backend/app/collection/doubao_browser.py backend/tests/collection/test_prompt_pacing.py backend/tests/collection/test_doubao_browser.py backend/tests/test_config.py
git commit -m "feat: persist conservative doubao pacing"
```

---

### Task 2: One Doubao Web Task per Account

**Files:**
- Create: `backend/app/collection/doubao_task_lease.py`
- Create: `backend/tests/collection/test_doubao_task_lease.py`
- Modify: `backend/app/collection/service.py:327-357, 562-583, 1113-1168`
- Modify: `backend/tests/collection/test_two_stage_service.py`

**Interfaces:**
- Produces: `DoubaoTaskLease.acquire(task_id, checkpoint)`, `release(task_id)`, `reserve(task_id)`, and read-only `owner`.
- `CollectionService` acquires the lease before claiming a task that has `use_doubao_web_chat=true`.
- A `TaskWaitingForLogin` exit retains ownership; all other task exits release it.

- [ ] **Step 1: Write failing lease unit tests**

```python
from threading import Event, Thread

from app.collection.doubao_task_lease import DoubaoTaskLease


def test_second_task_waits_until_owner_releases() -> None:
    lease = DoubaoTaskLease(wait_seconds=0.01)
    first_acquired = Event()
    second_acquired = Event()

    def first() -> None:
        lease.acquire("task-1", lambda: None)
        first_acquired.set()
        second_acquired.wait(timeout=0.05)
        lease.release("task-1")

    def second() -> None:
        first_acquired.wait(timeout=1)
        lease.acquire("task-2", lambda: None)
        second_acquired.set()
        lease.release("task-2")

    first_thread = Thread(target=first)
    second_thread = Thread(target=second)
    first_thread.start()
    second_thread.start()
    first_thread.join(timeout=1)
    second_thread.join(timeout=1)

    assert second_acquired.is_set()
    assert lease.owner is None


def test_reserved_challenge_owner_cannot_be_replaced() -> None:
    lease = DoubaoTaskLease(wait_seconds=0.01)
    lease.reserve("task-1")

    assert lease.owner == "task-1"
    lease.release("task-2")
    assert lease.owner == "task-1"
```

- [ ] **Step 2: Run the lease tests and verify RED**

Run:

```bash
conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/collection/test_doubao_task_lease.py -q
```

Expected: collection fails with `ModuleNotFoundError: No module named 'app.collection.doubao_task_lease'`.

- [ ] **Step 3: Implement `DoubaoTaskLease`**

```python
from __future__ import annotations

from collections.abc import Callable
from threading import Condition


class DoubaoTaskLease:
    def __init__(self, *, wait_seconds: float = 1.0) -> None:
        self._condition = Condition()
        self._owner: str | None = None
        self._wait_seconds = wait_seconds

    @property
    def owner(self) -> str | None:
        with self._condition:
            return self._owner

    def acquire(self, task_id: str, checkpoint: Callable[[], None]) -> None:
        with self._condition:
            while self._owner not in {None, task_id}:
                self._condition.wait(timeout=self._wait_seconds)
                checkpoint()
            self._owner = task_id

    def reserve(self, task_id: str) -> None:
        with self._condition:
            if self._owner is None:
                self._owner = task_id

    def release(self, task_id: str) -> None:
        with self._condition:
            if self._owner != task_id:
                return
            self._owner = None
            self._condition.notify_all()
```

- [ ] **Step 4: Integrate the lease into `CollectionService`**

Create `self._doubao_task_lease = DoubaoTaskLease()` in the constructor. Before `_claim_task(task_id)`, load task metadata and acquire only when `use_doubao_web_chat` is true:

```python
uses_web = bool(self.store.load(task_id, include_leads=False).get("use_doubao_web_chat"))
if uses_web:
    self._doubao_task_lease.acquire(
        task_id,
        lambda: self._control_checkpoint(task_id),
    )
retain_web_lease = False
try:
    self._run_scheduled_task_locked(task_id)
except TaskWaitingForLogin:
    retain_web_lease = True
    raise
finally:
    if uses_web and not retain_web_lease:
        self._doubao_task_lease.release(task_id)
```

Keep the existing exception handling inside `_run_scheduled_task_locked`; set its `TaskWaitingForLogin` branch to return a boolean indicating lease retention, or move that catch one level outward without changing task status behavior. When terminating a `waiting_for_login` task, call `release(task_id)`. During service initialization, reserve the oldest stored `waiting_for_login` web task so restart preserves challenge priority.

Add a service integration test that starts two web tasks with `max_concurrent_tasks=2`, blocks the first gateway call with an `Event`, and asserts the second task has made zero gateway calls until the first task completes or terminates.

- [ ] **Step 5: Run lease and service tests**

```bash
conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/collection/test_doubao_task_lease.py backend/tests/collection/test_two_stage_service.py -k "lease or waiting_for_login or scheduler" -q
```

Expected: all selected tests pass, and the two-task test records a maximum of one active web task.

- [ ] **Step 6: Commit single-account scheduling**

```bash
git add backend/app/collection/doubao_task_lease.py backend/app/collection/service.py backend/tests/collection/test_doubao_task_lease.py backend/tests/collection/test_two_stage_service.py
git commit -m "feat: serialize doubao web tasks by account"
```

---

### Task 3: Automatic Resume after Manual Verification

**Files:**
- Create: `backend/app/collection/doubao_attention.py`
- Create: `backend/tests/collection/test_doubao_attention.py`
- Modify: `backend/app/main.py:84-112`
- Modify: `backend/app/collection/service.py:527-560, 579-583`
- Modify: `backend/tests/collection/test_two_stage_service.py:1188-1229`

**Interfaces:**
- Produces: `DoubaoAttentionMonitor.start()`, `stop()`, and `check_once()`.
- Consumes: `waiting_task_ids()`, `refresh_status()`, `resume_task(task_id)`, `poll_seconds=60`, and `ready_confirmations=2`.
- The monitor performs read-only status checks; it never calls a prompt, page reload, browser restart, or CAPTCHA interaction.

- [ ] **Step 1: Write failing monitor tests**

```python
from app.collection.doubao_attention import DoubaoAttentionMonitor
from app.collection.doubao_browser import DoubaoBrowserStatus


def test_monitor_resumes_only_after_two_ready_checks() -> None:
    resumed: list[str] = []
    monitor = DoubaoAttentionMonitor(
        waiting_task_ids=lambda: ["task-1"],
        refresh_status=lambda: DoubaoBrowserStatus("ready", "ready", None),
        resume_task=resumed.append,
        poll_seconds=60,
        ready_confirmations=2,
    )

    monitor.check_once()
    assert resumed == []
    monitor.check_once()
    assert resumed == ["task-1"]


def test_monitor_resets_confirmation_when_challenge_remains() -> None:
    states = iter(["ready", "waiting_for_login", "ready", "ready"])
    resumed: list[str] = []
    monitor = DoubaoAttentionMonitor(
        waiting_task_ids=lambda: ["task-1"],
        refresh_status=lambda: DoubaoBrowserStatus(next(states), "status", None),
        resume_task=resumed.append,
        poll_seconds=60,
        ready_confirmations=2,
    )

    for _ in range(4):
        monitor.check_once()

    assert resumed == ["task-1"]
```

- [ ] **Step 2: Run monitor tests and verify RED**

```bash
conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/collection/test_doubao_attention.py -q
```

Expected: collection fails with `ModuleNotFoundError: No module named 'app.collection.doubao_attention'`.

- [ ] **Step 3: Implement the low-frequency monitor**

```python
from __future__ import annotations

from collections.abc import Callable
from threading import Event, Thread

from app.collection.doubao_browser import DoubaoBrowserStatus


class DoubaoAttentionMonitor:
    def __init__(
        self,
        *,
        waiting_task_ids: Callable[[], list[str]],
        refresh_status: Callable[[], DoubaoBrowserStatus],
        resume_task: Callable[[str], object],
        poll_seconds: float,
        ready_confirmations: int,
    ) -> None:
        self.waiting_task_ids = waiting_task_ids
        self.refresh_status = refresh_status
        self.resume_task = resume_task
        self.poll_seconds = max(1.0, poll_seconds)
        self.ready_confirmations = max(1, ready_confirmations)
        self._ready_counts: dict[str, int] = {}
        self._stop = Event()
        self._thread: Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = Thread(target=self._run, name="doubao-attention", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def check_once(self) -> None:
        waiting = self.waiting_task_ids()
        active = set(waiting)
        self._ready_counts = {
            task_id: count
            for task_id, count in self._ready_counts.items()
            if task_id in active
        }
        if not waiting:
            return
        owner = waiting[0]
        try:
            ready = self.refresh_status().state == "ready"
        except Exception:
            ready = False
        self._ready_counts[owner] = self._ready_counts.get(owner, 0) + 1 if ready else 0
        if self._ready_counts[owner] < self.ready_confirmations:
            return
        self.resume_task(owner)
        self._ready_counts.pop(owner, None)

    def _run(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            self.check_once()
```

Keep exception handling at the monitor boundary so a temporary CDP failure does not kill the thread. Log only the state transition; do not log page text, cookies, tokens, or screenshots.

- [ ] **Step 4: Wire monitor lifecycle and automatic resume**

In `main.py`, construct the monitor only when the gateway exists:

```python
doubao_attention_monitor = DoubaoAttentionMonitor(
    waiting_task_ids=lambda: store.list_task_ids_by_status(["waiting_for_login"]),
    refresh_status=doubao_web_gateway.refresh_status,
    resume_task=collection_service.resume_task,
    poll_seconds=resolved_settings.doubao_web_attention_poll_seconds,
    ready_confirmations=resolved_settings.doubao_web_ready_confirmations,
)
```

Start it during application lifespan after browser attachment begins, stop it before `collection_service.shutdown()`, and expose it on `app.state` for integration tests. Keep manual `resume_task` working as an immediate override.

Update the existing checkpoint test so it calls the monitor twice after setting `gateway.ready = True`, then asserts the task completes and the first company is not repeated.

- [ ] **Step 5: Run automatic-resume tests**

```bash
conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/collection/test_doubao_attention.py backend/tests/collection/test_two_stage_service.py -k "attention or login_checkpoint or terminate_from_web_chat" -q
```

Expected: all selected tests pass; no prompt is submitted during readiness checks.

- [ ] **Step 6: Commit automatic recovery**

```bash
git add backend/app/main.py backend/app/collection/doubao_attention.py backend/app/collection/service.py backend/tests/collection/test_doubao_attention.py backend/tests/collection/test_two_stage_service.py
git commit -m "feat: resume doubao tasks after manual verification"
```

---

### Task 4: User-Facing Status and Full Regression

**Files:**
- Modify: `frontend/src/labels.ts:10-20`
- Modify: `frontend/src/labels.test.ts:24-38`
- Modify: `frontend/src/App.tsx:218-230`
- Modify: `docs/superpowers/specs/2026-08-03-doubao-captcha-risk-reduction-design.md`

**Interfaces:**
- Consumes existing task status `waiting_for_login`, browser state `waiting_for_login`, `round_phase`, and `next_round_at`.
- Produces no new API fields.

- [ ] **Step 1: Write the failing frontend label test**

```typescript
test('explains that manual verification resumes automatically', () => {
  assert.equal(
    doubaoWebStateLabel('waiting_for_login'),
    '等待人工验证，完成后自动继续',
  )
})
```

Run:

```bash
conda run -n doubao-leads --no-capture-output npm.cmd --prefix frontend test -- labels.test.ts
```

Expected: FAIL because the current label is `等待登录或安全验证`.

- [ ] **Step 2: Implement status copy without adding controls**

Change the label mapping to:

```typescript
waiting_for_login: '等待人工验证，完成后自动继续',
```

Add this unframed message below the task controls:

```tsx
{task?.status === 'waiting_for_login' && (
  <p className="partial-note">
    请在当前专用 Chrome 完成豆包登录或安全验证；系统检测恢复后会从当前公司自动继续。
  </p>
)}
```

Do not add an automatic CAPTCHA-solving button or a second browser-launch button.

- [ ] **Step 3: Run frontend tests, typecheck, and build**

```bash
conda run -n doubao-leads --no-capture-output npm.cmd --prefix frontend test
conda run -n doubao-leads --no-capture-output npm.cmd --prefix frontend run typecheck
conda run -n doubao-leads --no-capture-output npm.cmd --prefix frontend run build
```

Expected: all commands exit with code 0.

- [ ] **Step 4: Run the complete backend suite**

```bash
conda run -n doubao-leads --no-capture-output python -m pytest backend/tests -q
```

Expected: all backend tests pass with zero failures.

- [ ] **Step 5: Verify runtime behavior without sending a Doubao prompt**

Restart only the local backend using the existing project script, then call the health and Doubao status endpoints:

```bash
conda run -n doubao-leads --no-capture-output python scripts/restart_local_server.py
conda run -n doubao-leads --no-capture-output python -c "import json, urllib.request; print(json.load(urllib.request.urlopen('http://127.0.0.1:8010/api/v1/health'))); print(json.load(urllib.request.urlopen('http://127.0.0.1:8010/api/v1/collection-tasks/doubao-web/status')))"
```

Expected: health returns `{"status": "ok"}` and the browser status endpoint returns a JSON object without creating a new Chrome profile or submitting a prompt.

- [ ] **Step 6: Update the design implementation note and validate UTF-8**

Append an implementation note listing the final defaults, state file, lease behavior, and monitor behavior. Decode both Markdown files as UTF-8 and assert none of the common mojibake code points `U+7487`, `U+93C2`, `U+93B5`, `U+935A`, `U+7ED4`, `U+6D93`, `U+9225`, `U+9286`, or the replacement character `U+FFFD` appear.

- [ ] **Step 7: Commit UI and verification documentation**

```bash
git add frontend/src/App.tsx frontend/src/labels.ts frontend/src/labels.test.ts docs/superpowers/specs/2026-08-03-doubao-captcha-risk-reduction-design.md
git commit -m "feat: show automatic doubao recovery status"
```

---

## Final Acceptance Checklist

- [ ] Persistent pacing state prevents a backend restart from causing an immediate prompt burst.
- [ ] Production defaults are 15～30 seconds, 20～25 prompts, and 20～30 minutes.
- [ ] Two simultaneous web tasks produce at most one active Doubao task.
- [ ] A challenged task retains account ownership and its company checkpoint.
- [ ] Readiness polling submits no prompt and automatically resumes after two successful checks.
- [ ] Manual resume, pause, terminate, continuous rounds, ERP delivery, and deduplication still pass regression tests.
- [ ] The persistent Chrome process and profile are reused.
- [ ] No CAPTCHA recognition, clicking, or bypass behavior exists.
- [ ] Backend tests, frontend tests, typecheck, and build all pass.
