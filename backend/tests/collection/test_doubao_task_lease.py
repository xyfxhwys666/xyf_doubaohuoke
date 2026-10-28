from threading import Event, Thread

import pytest

from app.collection.doubao_task_lease import DoubaoTaskLease, DoubaoTaskLeaseClosed


def test_second_task_waits_until_owner_releases() -> None:
    lease = DoubaoTaskLease(wait_seconds=0.01)
    first_acquired = Event()
    allow_first_release = Event()
    second_acquired = Event()

    def first() -> None:
        lease.acquire("task-1", lambda: None)
        first_acquired.set()
        allow_first_release.wait(timeout=1)
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

    assert first_acquired.wait(timeout=1)
    assert not second_acquired.wait(timeout=0.05)
    allow_first_release.set()
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


def test_shutdown_wakes_waiting_task_without_granting_account() -> None:
    lease = DoubaoTaskLease(wait_seconds=0.01)
    lease.reserve("task-1")
    stopped = Event()

    def wait_for_account() -> None:
        with pytest.raises(DoubaoTaskLeaseClosed):
            lease.acquire("task-2", lambda: None)
        stopped.set()

    thread = Thread(target=wait_for_account)
    thread.start()
    lease.shutdown()
    thread.join(timeout=1)

    assert stopped.is_set()
    assert lease.owner is None
