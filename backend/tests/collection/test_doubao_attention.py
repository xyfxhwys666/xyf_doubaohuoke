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


def test_monitor_treats_temporary_status_failure_as_not_ready() -> None:
    attempts = 0
    resumed: list[str] = []

    def refresh_status() -> DoubaoBrowserStatus:
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise RuntimeError("temporary CDP failure")
        return DoubaoBrowserStatus("ready", "ready", None)

    monitor = DoubaoAttentionMonitor(
        waiting_task_ids=lambda: ["task-1"],
        refresh_status=refresh_status,
        resume_task=resumed.append,
        poll_seconds=60,
        ready_confirmations=2,
    )

    for _ in range(4):
        monitor.check_once()

    assert resumed == ["task-1"]
