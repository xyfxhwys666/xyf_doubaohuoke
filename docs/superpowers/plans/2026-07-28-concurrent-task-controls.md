# 并行采集任务控制与 ERP 自动推送实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不引入 PostgreSQL、Redis 或 Celery 的前提下，让最多两个采集任务并行运行，展示全部任务，并为每个任务提供暂停、继续、终止和可选 ERP 自动推送。

**Architecture:** `CollectionService` 继续负责单个任务的采集流程，新建 `TaskScheduler` 用固定为 2 的工作线程管理任务队列。任务状态、控制标记、检查点和逐条 ERP 推送状态继续保存在现有任务 JSON 中；前端轮询任务列表并按所选任务加载结果。

**Tech Stack:** Python 3.12、FastAPI、`concurrent.futures.ThreadPoolExecutor`、本地 JSON、pytest、React、TypeScript、Vite。

## Global Constraints

- 默认最多同时运行两个采集任务，第三个及后续任务按创建时间先进先出排队。
- 新建任务不得替换、隐藏、暂停或终止旧任务。
- 暂停和终止采用协作式检查点；已经发出的单次模型或 ERP 请求允许完成。
- 暂停、终止和 ERP 失败不得删除已采集线索。
- 继续执行不得重复已经完成的城市发现或已经完成的公司补全。
- ERP 自动推送默认关闭，开启时也必须先保存并展示线索，再逐条推送。
- 不新增 PostgreSQL、Redis、Celery、Docker 服务或前端测试框架。
- 后端测试和运行使用 `doubao-leads` conda 环境。

---

### Task 1: 持久化任务生命周期与原子 JSON 更新

**Files:**
- Modify: `backend/app/collection/service.py`
- Test: `backend/tests/collection/test_task_scheduler.py`

**Interfaces:**
- Produces: `TaskStore.list_tasks() -> list[dict[str, Any]]`
- Produces: `TaskStore.update(task_id, mutation) -> dict[str, Any]`
- Produces: 新任务字段 `auto_deliver`、`pause_requested`、`terminate_requested`、生命周期时间和检查点字段。
- Produces: 任务摘要字段 `contact_lead_count`，统计至少具备手机、邮箱或微信之一的线索。

- [ ] **Step 1: 写出任务列表排序、旧任务默认值和原子更新失败测试**

```python
def test_list_tasks_returns_newest_first_and_backfills_lifecycle_defaults(tmp_path: Path) -> None:
    store = TaskStore(tmp_path)
    first = _stored_task("first", created_at="2026-07-28T01:00:00+00:00")
    second = _stored_task("second", created_at="2026-07-28T02:00:00+00:00")
    store.save(first)
    store.save(second)

    tasks = store.list_tasks()

    assert [task["id"] for task in tasks] == ["second", "first"]
    assert tasks[0]["auto_deliver"] is False
    assert tasks[0]["pause_requested"] is False
    assert tasks[0]["terminate_requested"] is False


def test_update_mutates_one_task_without_losing_existing_leads(tmp_path: Path) -> None:
    store = TaskStore(tmp_path)
    task = _stored_task("task-1")
    task["leads"] = [{"city": "武汉市", "company_name": "甲公司"}]
    store.save(task)

    updated = store.update("task-1", lambda current: current.update(status="paused"))

    assert updated["status"] == "paused"
    assert updated["leads"] == task["leads"]
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/collection/test_task_scheduler.py -v`

Expected: FAIL，提示 `TaskStore` 尚无 `list_tasks` 或 `update`。

- [ ] **Step 3: 在 `TaskStore` 中实现同一把 `RLock` 下的读改写，并统一补齐旧任务默认字段**

```python
TASK_DEFAULTS: dict[str, Any] = {
    "auto_deliver": False,
    "pause_requested": False,
    "terminate_requested": False,
    "started_at": None,
    "paused_at": None,
    "terminated_at": None,
    "completed_at": None,
}

def _with_task_defaults(task: dict[str, Any]) -> dict[str, Any]:
    for key, value in TASK_DEFAULTS.items():
        task.setdefault(key, value)
    return task

def update(self, task_id: str, mutation: Callable[[dict[str, Any]], Any]) -> dict[str, Any]:
    with self._lock:
        task = self.load(task_id)
        mutation(task)
        self.save(task)
        return task

def list_tasks(self) -> list[dict[str, Any]]:
    with self._lock:
        tasks = [_with_task_defaults(json.loads(path.read_text(encoding="utf-8"))) for path in self.tasks_dir.glob("*.json")]
    return sorted(tasks, key=lambda task: task.get("created_at", ""), reverse=True)
```

同时把 `save_leads` 和 `reconcile_leads` 的完整读改写过程放入同一把锁，避免并行补全覆盖其他线程刚保存的线索。
`task_summary()` 从当前任务线索计算 `contact_lead_count`；列表读取完整任务后生成摘要，确保每个任务卡片都能显示有效联系方式数量。

- [ ] **Step 4: 运行目标测试和现有两阶段保存测试**

Run: `conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/collection/test_task_scheduler.py backend/tests/collection/test_two_stage_service.py -v`

Expected: PASS。

- [ ] **Step 5: 提交原子持久化改动**

```bash
git add backend/app/collection/service.py backend/tests/collection/test_task_scheduler.py
git commit -m "feat: persist collection task lifecycle"
```

---

### Task 2: 两任务调度器与暂停、继续、终止控制

**Files:**
- Create: `backend/app/collection/scheduler.py`
- Modify: `backend/app/collection/service.py`
- Modify: `backend/app/config.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/collection/test_task_scheduler.py`

**Interfaces:**
- Consumes: `TaskStore.update()` 和持久化生命周期字段。
- Produces: `TaskScheduler.submit(task_id) -> bool`
- Produces: `CollectionService.start_task(task_id) -> dict[str, Any]`
- Produces: `CollectionService.pause_task(task_id) -> dict[str, Any]`
- Produces: `CollectionService.resume_task(task_id) -> dict[str, Any]`
- Produces: `CollectionService.terminate_task(task_id) -> dict[str, Any]`
- Produces: `CollectionService.shutdown() -> None`

- [ ] **Step 1: 写出最多两个并行、第三个排队和任务互不替换的失败测试**

```python
def test_scheduler_runs_two_tasks_and_keeps_third_queued(tmp_path: Path) -> None:
    probe = BlockingRunner()
    service = _service(tmp_path, runner_probe=probe, max_concurrent_tasks=2)
    task_ids = [_create_and_start(service, f"task-{index}") for index in range(3)]

    assert probe.wait_until_active(2)
    assert service.get_task(task_ids[0])["status"] == "running"
    assert service.get_task(task_ids[1])["status"] == "running"
    assert service.get_task(task_ids[2])["status"] == "queued"

    probe.release(task_ids[0])
    assert probe.wait_until_started(task_ids[2])
    service.shutdown()
```

- [ ] **Step 2: 写出暂停释放名额、继续重新排队、终止保留结果的失败测试**

```python
def test_pause_resume_and_terminate_are_cooperative_and_persistent(tmp_path: Path) -> None:
    service, task_id = _running_checkpoint_service(tmp_path)

    pausing = service.pause_task(task_id)
    assert pausing["status"] == "pausing"
    assert _wait_for_status(service, task_id, "paused")
    assert service.get_task(task_id)["leads"]

    resumed = service.resume_task(task_id)
    assert resumed["status"] in {"queued", "running"}
    terminating = service.terminate_task(task_id)
    assert terminating["status"] in {"terminating", "terminated"}
    assert _wait_for_status(service, task_id, "terminated")
    assert service.get_task(task_id)["leads"]
```

- [ ] **Step 3: 运行调度测试并确认失败**

Run: `conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/collection/test_task_scheduler.py -v`

Expected: FAIL，调度器和控制方法尚不存在。

- [ ] **Step 4: 实现固定工作线程调度器**

```python
class TaskScheduler:
    def __init__(self, runner: Callable[[str], None], *, max_workers: int = 2) -> None:
        self._runner = runner
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="collection-task")
        self._submitted: set[str] = set()
        self._lock = RLock()

    def submit(self, task_id: str) -> bool:
        with self._lock:
            if task_id in self._submitted:
                return False
            self._submitted.add(task_id)
            future = self._executor.submit(self._run, task_id)
            future.add_done_callback(lambda _: self._discard(task_id))
            return True

    def _run(self, task_id: str) -> None:
        self._runner(task_id)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=False)
```

工作线程真正开始时，`CollectionService` 才把 `queued` 原子更新为 `running`；这样执行器内部等待的任务仍准确显示为排队。

- [ ] **Step 5: 移除全局 `_run_lock`，加入控制检查点和恢复逻辑**

```python
class TaskPaused(RuntimeError):
    pass

class TaskTerminated(RuntimeError):
    pass

def _control_checkpoint(self, task_id: str) -> None:
    task = self.store.load(task_id, include_leads=False)
    if task.get("terminate_requested"):
        raise TaskTerminated(task_id)
    if task.get("pause_requested"):
        raise TaskPaused(task_id)
```

在城市发现前后、每个模型请求前后、公司补全前后和 ERP 推送前后调用检查点。调度包装器捕获控制异常并持久化为 `paused` 或 `terminated`；正常结束写入 `completed_at`。`_recover_interrupted_tasks()` 将无控制标记的旧 `running` 任务改为 `queued` 后重新提交，而不是直接标记失败。

- [ ] **Step 6: 接入配置和 FastAPI 生命周期关闭**

```python
class Settings(BaseSettings):
    collection_max_concurrent_tasks: int = 2

collection_service = CollectionService(
    store,
    build_providers(resolved_settings),
    build_model_verifiers(resolved_settings),
    lead_delivery=erp_delivery,
    max_concurrent_tasks=resolved_settings.collection_max_concurrent_tasks,
)

finally:
    collection_service.shutdown()
    engine.dispose()
```

- [ ] **Step 7: 运行调度器、两阶段采集和重启恢复测试**

Run: `conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/collection/test_task_scheduler.py backend/tests/collection/test_two_stage_service.py -v`

Expected: PASS，测试退出时没有遗留工作线程。

- [ ] **Step 8: 提交调度器改动**

```bash
git add backend/app/collection/scheduler.py backend/app/collection/service.py backend/app/config.py backend/app/main.py backend/tests/collection/test_task_scheduler.py
git commit -m "feat: run and control concurrent collection tasks"
```

---

### Task 3: 任务列表与控制 API

**Files:**
- Modify: `backend/app/api/routes/collection_tasks.py`
- Test: `backend/tests/api/test_collection_task_controls.py`

**Interfaces:**
- Consumes: Task 2 的 `start_task`、`pause_task`、`resume_task` 和 `terminate_task`。
- Produces: `GET /api/v1/collection-tasks`
- Produces: `POST /api/v1/collection-tasks/{task_id}/pause`
- Produces: `POST /api/v1/collection-tasks/{task_id}/resume`
- Produces: `POST /api/v1/collection-tasks/{task_id}/terminate`

- [ ] **Step 1: 写出列表、幂等控制和非法状态的 API 失败测试**

```python
def test_task_list_and_control_endpoints(collection_client: TestClient) -> None:
    first = create_natural_language_task(collection_client, "找武汉软装公司")
    second = create_natural_language_task(collection_client, "找长沙整装公司")

    listed = collection_client.get("/api/v1/collection-tasks")
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["items"]][:2] == [second["id"], first["id"]]

    assert collection_client.post(f"/api/v1/collection-tasks/{first['id']}/pause").status_code == 202
    assert collection_client.post(f"/api/v1/collection-tasks/{first['id']}/pause").status_code == 202
    assert collection_client.post(f"/api/v1/collection-tasks/{first['id']}/resume").status_code == 202
    assert collection_client.post(f"/api/v1/collection-tasks/{first['id']}/terminate").status_code == 202
```

- [ ] **Step 2: 运行 API 测试并确认失败**

Run: `conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/api/test_collection_task_controls.py -v`

Expected: FAIL，列表和控制路由返回 404。

- [ ] **Step 3: 将启动和控制统一交给服务调度器**

```python
@router.get("/collection-tasks")
def list_collection_tasks(request: Request) -> dict:
    service = _service(request)
    return {"items": [service.task_summary(task) for task in service.list_tasks()]}

@router.post("/collection-tasks/{task_id}/pause", status_code=status.HTTP_202_ACCEPTED)
def pause_collection_task(task_id: str, request: Request) -> dict:
    return _control_response(lambda: _service(request).pause_task(task_id))

@router.post("/collection-tasks/{task_id}/resume", status_code=status.HTTP_202_ACCEPTED)
def resume_collection_task(task_id: str, request: Request) -> dict:
    return _control_response(lambda: _service(request).resume_task(task_id))

@router.post("/collection-tasks/{task_id}/terminate", status_code=status.HTTP_202_ACCEPTED)
def terminate_collection_task(task_id: str, request: Request) -> dict:
    return _control_response(lambda: _service(request).terminate_task(task_id))
```

删除这些路由中的 `BackgroundTasks` 调度；`start`、错误重试和重新补全都调用服务的队列入口。不存在任务返回 404，已完成任务的暂停或继续返回 409，重复暂停和重复终止返回当前状态。

- [ ] **Step 4: 运行 API 和原有 MVP 测试**

Run: `conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/api/test_collection_task_controls.py backend/tests/api/test_collection_mvp.py -v`

Expected: PASS。

- [ ] **Step 5: 提交任务控制 API**

```bash
git add backend/app/api/routes/collection_tasks.py backend/tests/api/test_collection_task_controls.py
git commit -m "feat: expose collection task controls"
```

---

### Task 4: 任务级 ERP 自动推送和逐条状态

**Files:**
- Modify: `backend/app/api/routes/collection_tasks.py`
- Modify: `backend/app/collection/service.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/collection/exports.py`
- Test: `backend/tests/collection/test_two_stage_service.py`
- Test: `backend/tests/api/test_collection_task_controls.py`

**Interfaces:**
- Produces: `GET /api/v1/delivery-config -> {"configured": bool}`
- Extends: 自然语言创建请求 `auto_deliver: bool = False`
- Extends: 线索字段 `delivery_status`、`delivery_error`、`delivered_at`

- [ ] **Step 1: 写出默认关闭、开启时先保存再推送和失败不中断采集的失败测试**

```python
def test_auto_delivery_is_off_by_default(collection_client: TestClient) -> None:
    task = create_natural_language_task(collection_client, "找武汉软装公司")
    assert task["auto_deliver"] is False


def test_enabled_auto_delivery_persists_each_status(tmp_path: Path) -> None:
    store = RecordingStore(tmp_path)
    delivery = BlockingRecordingDelivery(store)
    service = _two_stage_service(store, delivery=delivery)
    task = service.create_task({**_payload(["deepseek"]), "auto_deliver": True})

    service.start_task(task["id"])
    assert delivery.wait_until_sending()
    sending = store.list_leads(task["id"])[0]
    assert sending["delivery_status"] == "sending"

    delivery.release()
    assert _wait_for_task_completion(service, task["id"])
    assert any(lead["delivery_status"] == "succeeded" for lead in store.list_leads(task["id"]))
```

- [ ] **Step 2: 运行目标测试并确认失败**

Run: `conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/collection/test_two_stage_service.py backend/tests/api/test_collection_task_controls.py -v`

Expected: FAIL，创建请求和线索尚无自动推送字段。

- [ ] **Step 3: 实现配置接口和任务开关校验**

```python
class NaturalLanguageCollectionTaskRequest(BaseModel):
    request_text: str = Field(min_length=1, max_length=4000)
    models: list[str] = Field(min_length=1)
    auto_deliver: bool = False

@router.get("/delivery-config")
def delivery_config(request: Request) -> dict[str, bool]:
    return {"configured": request.app.state.erp_delivery.configured}
```

只有 `auto_deliver=true` 且 ERP 已配置时允许创建任务；旧任务默认关闭。

- [ ] **Step 4: 在逐条推送前后持久化状态**

```python
def _deliver_lead(self, task: dict[str, Any], lead: dict[str, Any]) -> None:
    if not task.get("auto_deliver") or not _has_contact(lead):
        lead["delivery_status"] = "display_only"
        self.store.save_leads(task["id"], [lead])
        return
    lead.update(delivery_status="pending", delivery_error=None, delivered_at=None)
    self.store.save_leads(task["id"], [lead])
    lead.update(delivery_status="sending", delivery_error=None, delivered_at=None)
    self.store.save_leads(task["id"], [lead])
    result = self.lead_delivery.deliver(task, lead)
    lead["delivery_status"] = "succeeded" if result.status in {"completed", "skipped"} else "failed"
    lead["delivery_error"] = result.error
    lead["delivered_at"] = _now() if lead["delivery_status"] == "succeeded" else None
    self.store.save_leads(task["id"], [lead])
```

ERP 推送继续在单任务采集线程内逐条执行；异常转换为 `failed`，不得向外抛出阻断采集。导出字段追加 ERP 状态、失败原因和成功时间。

- [ ] **Step 5: 运行 ERP、两阶段和导出回归测试**

Run: `conda run -n doubao-leads --no-capture-output python -m pytest backend/tests/collection/test_erp_delivery.py backend/tests/collection/test_two_stage_service.py backend/tests/api/test_collection_mvp.py -v`

Expected: PASS。

- [ ] **Step 6: 提交自动推送改动**

```bash
git add backend/app/api/routes/collection_tasks.py backend/app/collection/service.py backend/app/main.py backend/app/collection/exports.py backend/tests/collection/test_two_stage_service.py backend/tests/api/test_collection_task_controls.py backend/tests/api/test_collection_mvp.py
git commit -m "feat: add task-level ERP auto delivery"
```

---

### Task 5: React 多任务列表、控制按钮和 ERP 状态展示

**Files:**
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/styles.css`

**Interfaces:**
- Consumes: Task 3 和 Task 4 的任务列表、控制和配置接口。
- Produces: 可切换任务列表、暂停/继续/终止按钮、自动推送开关和结果表 ERP 状态列。

- [ ] **Step 1: 扩展前端类型和 API 客户端**

```typescript
export type TaskStatus = 'draft' | 'queued' | 'running' | 'pausing' | 'paused'
  | 'completed' | 'completed_with_errors' | 'terminating' | 'terminated'

export type DeliveryStatus = 'display_only' | 'pending' | 'sending' | 'succeeded' | 'failed'

tasks: () => request<{ items: CollectionTask[] }>('/collection-tasks'),
pauseTask: (id: string) => request<CollectionTask>(`/collection-tasks/${id}/pause`, { method: 'POST' }),
resumeTask: (id: string) => request<CollectionTask>(`/collection-tasks/${id}/resume`, { method: 'POST' }),
terminateTask: (id: string) => request<CollectionTask>(`/collection-tasks/${id}/terminate`, { method: 'POST' }),
deliveryConfig: () => request<{ configured: boolean }>('/delivery-config'),
```

修正 `request()`：只在响应体为空时返回 `undefined`，不能因为状态码为 202 就丢弃控制接口返回的任务摘要。

- [ ] **Step 2: 将单任务状态改为任务列表和当前任务 ID**

```typescript
const [tasks, setTasks] = useState<CollectionTask[]>([])
const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null)
const task = tasks.find((item) => item.id === selectedTaskId) ?? null

const refreshTasks = async () => {
  const { items } = await api.tasks()
  setTasks(items)
  setSelectedTaskId((current) => current && items.some((item) => item.id === current) ? current : items[0]?.id ?? null)
}
```

页面每 1.5 秒刷新任务摘要；只为当前选中任务加载线索。创建任务后把新任务选中，但保留旧任务列表。

- [ ] **Step 3: 增加紧凑任务列表和状态对应控制按钮**

```tsx
<button title="暂停任务" disabled={task.status !== 'running'} onClick={() => control(task.id, 'pause')}>
  <Pause size={16} />暂停
</button>
<button title="继续任务" disabled={task.status !== 'paused'} onClick={() => control(task.id, 'resume')}>
  <Play size={16} />继续
</button>
<button title="终止任务" disabled={!canTerminate(task.status)} onClick={() => control(task.id, 'terminate')}>
  <Square size={16} />终止
</button>
```

任务列表展示需求摘要、状态、创建时间、城市完成数、候选数和有效联系方式数。状态颜色区分排队、运行、暂停、完成、有异常和终止；控制提交期间对应按钮禁用。

- [ ] **Step 4: 增加默认关闭的 ERP 自动推送开关和结果状态列**

```tsx
<label className={`toggle-row ${deliveryConfigured ? '' : 'disabled'}`}>
  <input type="checkbox" checked={autoDeliver} disabled={!deliveryConfigured} onChange={(event) => setAutoDeliver(event.target.checked)} />
  <span>自动推送 ERP</span>
  <small>{deliveryConfigured ? (autoDeliver ? '有效线索逐条推送' : '只保存并展示') : 'ERP 未配置'}</small>
</label>
```

创建任务时提交 `auto_deliver`。结果表新增“ERP 推送”列，状态显示“仅展示、待推送、推送中、推送成功、推送失败”；失败原因放在 `title` 提示中。

- [ ] **Step 5: 调整响应式布局并执行生产构建**

Run: `conda run -n doubao-leads --no-capture-output npm.cmd --prefix frontend run build`

Expected: TypeScript 编译和 Vite 构建成功，任务列表及控制按钮在桌面和窄屏下不溢出。

- [ ] **Step 6: 提交前端多任务界面**

```bash
git add frontend/src/types.ts frontend/src/api.ts frontend/src/App.tsx frontend/src/styles.css
git commit -m "feat: add concurrent task controls to dashboard"
```

---

### Task 6: 全量验证、浏览器验收和配置文档

**Files:**
- Modify: `.env.example`
- Modify: `README.md` only if it exists at execution time; otherwise do not create it solely for this feature.

**Interfaces:**
- Consumes: Tasks 1-5 的完整功能。
- Produces: 可运行、可回归、可人工验收的最终版本。

- [ ] **Step 1: 补充并发配置示例**

```dotenv
COLLECTION_MAX_CONCURRENT_TASKS=2
```

ERP 密钥和现有本地 `.env` 不进入 Git。

- [ ] **Step 2: 运行后端全量测试和 Ruff**

Run: `conda run -n doubao-leads --no-capture-output python -m pytest backend/tests -q`

Expected: 全部测试 PASS。

Run: `conda run -n doubao-leads --no-capture-output python -m ruff check backend/app backend/tests`

Expected: `All checks passed!`

- [ ] **Step 3: 运行前端生产构建**

Run: `conda run -n doubao-leads --no-capture-output npm.cmd --prefix frontend run build`

Expected: Vite 构建成功。

- [ ] **Step 4: 启动本地服务并执行浏览器验收**

使用项目现有启动方式重启后端 `127.0.0.1:8010` 和前端 `127.0.0.1:5173`，确认：

1. 连续创建三个任务时两个显示“采集中”，第三个显示“排队中”。
2. 暂停任一运行任务后，排队任务开始；暂停任务结果仍可查看。
3. 继续暂停任务后，它进入排队或运行；终止后已有结果仍可下载。
4. 切换任务时看到各自独立的需求、进度、结果和 ERP 推送状态。
5. 自动推送默认关闭；开启后线索先出现，再显示推送中及最终状态。
6. 桌面和移动宽度下没有文字、按钮或表格异常重叠。

- [ ] **Step 5: 提交配置和最终修正**

```bash
git add .env.example backend frontend
git commit -m "chore: finalize concurrent collection delivery"
```
