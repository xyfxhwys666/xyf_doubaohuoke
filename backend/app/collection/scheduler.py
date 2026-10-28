from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from threading import RLock


class TaskScheduler:
    def __init__(self, runner: Callable[[str], None], *, max_workers: int = 2) -> None:
        self._runner = runner
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="collection-task",
        )
        self._submitted: set[str] = set()
        self._resubmit_requested: set[str] = set()
        self._cancelled_resubmissions: set[str] = set()
        self._lock = RLock()
        self._shutdown = False

    def submit(self, task_id: str, *, resubmit_if_running: bool = False) -> bool:
        with self._lock:
            if self._shutdown or task_id in self._cancelled_resubmissions:
                return False
            if task_id in self._submitted:
                if resubmit_if_running:
                    self._resubmit_requested.add(task_id)
                return False
            self._submitted.add(task_id)
            future = self._executor.submit(self._run, task_id)
            future.add_done_callback(
                lambda completed, submitted_id=task_id: self._discard(
                    submitted_id,
                    completed,
                )
            )
            return True

    def cancel_resubmit(self, task_id: str) -> None:
        with self._lock:
            self._resubmit_requested.discard(task_id)
            self._cancelled_resubmissions.add(task_id)

    def _run(self, task_id: str) -> None:
        self._runner(task_id)

    def _discard(self, task_id: str, _: Future[None]) -> None:
        should_resubmit = False
        with self._lock:
            self._submitted.discard(task_id)
            cancelled = task_id in self._cancelled_resubmissions
            if task_id in self._resubmit_requested and not cancelled:
                self._resubmit_requested.discard(task_id)
                should_resubmit = not self._shutdown
            self._cancelled_resubmissions.discard(task_id)
        if should_resubmit:
            submitted = self.submit(task_id)
            if not submitted:
                with self._lock:
                    self._cancelled_resubmissions.discard(task_id)

    def shutdown(self) -> None:
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
            self._resubmit_requested.clear()
            self._cancelled_resubmissions.clear()
        self._executor.shutdown(wait=False, cancel_futures=True)
