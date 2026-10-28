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


def build_pacer(state_path: Path, clock: FakeClock) -> PersistentPromptPacer:
    return PersistentPromptPacer(
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


def test_prompt_pacing_survives_gateway_restart(tmp_path: Path) -> None:
    state_path = tmp_path / "doubao-pacing.json"
    clock = FakeClock()
    first = build_pacer(state_path, clock)
    first.wait_for_slot()
    first.mark_submitted()

    restarted = build_pacer(state_path, clock)
    restarted.wait_for_slot()

    assert clock.delays == [15]


def test_prompt_pacing_uses_persisted_batch_cooldown(tmp_path: Path) -> None:
    state_path = tmp_path / "doubao-pacing.json"
    clock = FakeClock()
    pacer = build_pacer(state_path, clock)
    for _ in range(2):
        pacer.wait_for_slot()
        pacer.mark_submitted()

    restarted = build_pacer(state_path, clock)
    restarted.wait_for_slot()

    assert clock.delays == [15, 1200]


def test_prompt_pacing_reports_remaining_slot_wait(tmp_path: Path) -> None:
    state_path = tmp_path / "doubao-pacing.json"
    clock = FakeClock()
    pacer = build_pacer(state_path, clock)

    for _ in range(2):
        pacer.mark_submitted()

    assert pacer.seconds_until_slot() == 1200
