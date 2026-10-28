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
        self._stop.clear()
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
        self._ready_counts[owner] = (
            self._ready_counts.get(owner, 0) + 1 if ready else 0
        )
        if self._ready_counts[owner] < self.ready_confirmations:
            return
        try:
            self.resume_task(owner)
        except Exception:
            self._ready_counts[owner] = 0
            return
        self._ready_counts.pop(owner, None)

    def _run(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            self.check_once()
