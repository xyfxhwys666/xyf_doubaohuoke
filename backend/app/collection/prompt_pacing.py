from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from random import randint, uniform
from threading import Event, Thread
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
        sleep_fn: Callable[[float], None] | None = None,
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
        self._uses_default_sleep = sleep_fn is None
        self.sleep_fn = sleep_fn or sleep
        self.uniform_fn = uniform_fn
        self.randint_fn = randint_fn
        self.state = self._load()

    def wait_for_slot(self, *, cancel_event: Event | None = None) -> bool:
        remaining = self.seconds_until_slot()
        if remaining > 0:
            if cancel_event is not None:
                if self._uses_default_sleep:
                    if cancel_event.wait(remaining):
                        return False
                else:
                    finished = Event()
                    errors: list[BaseException] = []

                    def sleep_with_callback() -> None:
                        try:
                            self.sleep_fn(remaining)
                        except Exception as exc:  # pragma: no cover - callback contract
                            errors.append(exc)
                        finally:
                            finished.set()

                    Thread(target=sleep_with_callback, daemon=True).start()
                    while not finished.wait(0.05):
                        if cancel_event.is_set():
                            return False
                    if errors:
                        raise errors[0]
            else:
                self.sleep_fn(remaining)
        if self.state.cooldown_until is not None and self.now_fn() >= self.state.cooldown_until:
            self.state = PromptPacingState(
                batch_count=0,
                batch_target=self.randint_fn(self.batch_size_min, self.batch_size_max),
            )
            self._save()
        return True

    def seconds_until_slot(self) -> float:
        """Return the persisted pacing delay without blocking the caller."""
        target = max(
            self.state.next_prompt_at or 0.0,
            self.state.cooldown_until or 0.0,
        )
        return max(0.0, target - self.now_fn())

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
                    next_prompt_at=(
                        float(raw["next_prompt_at"])
                        if raw.get("next_prompt_at") is not None
                        else None
                    ),
                    cooldown_until=(
                        float(raw["cooldown_until"])
                        if raw.get("cooldown_until") is not None
                        else None
                    ),
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
