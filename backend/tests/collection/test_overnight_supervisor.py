from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path


def _load_supervisor_module():
    root = Path(__file__).resolve().parents[3]
    path = root / ".planning" / "overnight_goal_supervisor.py"
    spec = importlib.util.spec_from_file_location("overnight_goal_supervisor", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_watchdog_restarts_stale_working_worker_outside_pacing(monkeypatch) -> None:
    module = _load_supervisor_module()
    restarted: list[bool] = []
    stale_time = (datetime.now(UTC) - timedelta(minutes=25)).isoformat()

    monkeypatch.setattr(module, "pacing_blocks_prompt", lambda: False)
    monkeypatch.setattr(module, "restart_backend", lambda: restarted.append(True))
    monkeypatch.setattr(module, "log", lambda *_args, **_kwargs: None)

    module.watchdog(
        {"id": "task-1", "status": "running", "updated_at": stale_time},
        {"state": "working", "detail": "正在查询豆包网页", "current_company": "测试公司"},
    )

    assert restarted == [True]


def test_requeues_incomplete_continuous_task_when_units_report_complete(monkeypatch) -> None:
    module = _load_supervisor_module()
    calls: list[str] = []

    def request_json(path: str, **_kwargs):
        calls.append(path)
        return {}

    monkeypatch.setattr(module, "request_json", request_json)
    monkeypatch.setattr(module, "log", lambda *_args, **_kwargs: None)

    task = {
        "id": "task-1",
        "status": "completed_with_errors",
        "continuous_mode": True,
        "valid_lead_target": 100,
        "contact_lead_count": 4,
        "completed_units": 1,
        "total_units": 1,
    }
    state = {"last_retry_at": 0.0}

    assert module.recover_task(task, {"state": "working"}, state) is True
    assert calls == ["/collection-tasks/task-1/retry-errors"]


def test_request_json_allows_a_long_timeout_for_batch_delivery(monkeypatch) -> None:
    module = _load_supervisor_module()
    captured: dict[str, float] = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b"{}"

    def fake_urlopen(_request, *, timeout):
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(module, "urlopen", fake_urlopen)

    assert module.request_json("/health", timeout_seconds=900) == {}
    assert captured["timeout"] == 900
