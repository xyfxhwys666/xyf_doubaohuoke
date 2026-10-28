from __future__ import annotations

from collections.abc import Callable
from threading import Condition


class DoubaoTaskLeaseClosed(RuntimeError):
    pass


class DoubaoTaskLease:
    def __init__(self, *, wait_seconds: float = 1.0) -> None:
        self._condition = Condition()
        self._owner: str | None = None
        self._wait_seconds = wait_seconds
        self._closed = False

    @property
    def owner(self) -> str | None:
        with self._condition:
            return self._owner

    def acquire(self, task_id: str, checkpoint: Callable[[], None]) -> None:
        with self._condition:
            if self._closed:
                raise DoubaoTaskLeaseClosed("Doubao task lease is closed")
            while self._owner not in {None, task_id}:
                self._condition.wait(timeout=self._wait_seconds)
                if self._closed:
                    raise DoubaoTaskLeaseClosed("Doubao task lease is closed")
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

    def shutdown(self) -> None:
        with self._condition:
            self._closed = True
            self._owner = None
            self._condition.notify_all()
