from pathlib import Path

import pytest

from app.collection.dedicated_chrome import (
    DedicatedChromeLauncher,
    DedicatedChromeUnavailable,
)


def test_start_reuses_existing_local_cdp_without_spawning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = DedicatedChromeLauncher(
        tmp_path / "profile",
        "https://www.doubao.com/chat/",
        executable=Path("C:/Chrome/chrome.exe"),
    )
    monkeypatch.setattr(launcher, "is_running", lambda: True)
    spawned: list[list[str]] = []
    monkeypatch.setattr(launcher, "_spawn", lambda command: spawned.append(command))

    assert launcher.start() is False
    assert spawned == []


def test_start_uses_fixed_profile_and_loopback_cdp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "chrome.exe"
    executable.touch()
    launcher = DedicatedChromeLauncher(
        tmp_path / "profile",
        "https://www.doubao.com/chat/",
        executable=executable,
        cdp_port=9333,
    )
    checks = iter([False, True])
    monkeypatch.setattr(launcher, "is_running", lambda: next(checks))
    monkeypatch.setattr(launcher, "_cdp_endpoint_responds", lambda: False)
    commands: list[list[str]] = []
    monkeypatch.setattr(launcher, "_spawn", lambda command: commands.append(command))

    assert launcher.start() is True
    assert "--remote-debugging-address=127.0.0.1" in commands[0]
    assert "--remote-debugging-port=9333" in commands[0]
    assert f"--user-data-dir={tmp_path / 'profile'}" in commands[0]
    assert "--disable-gpu" in commands[0]
    assert "--disable-gpu-compositing" in commands[0]
    assert "--disable-gpu-rasterization" in commands[0]
    assert "--disable-gpu-sandbox" in commands[0]
    assert "--in-process-gpu" in commands[0]
    assert "--use-gl=swiftshader" in commands[0]
    assert commands[0][-1] == "https://www.doubao.com/chat/"


def test_launcher_rejects_non_loopback_cdp_host(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="127.0.0.1"):
        DedicatedChromeLauncher(
            tmp_path / "profile",
            "https://www.doubao.com/chat/",
            cdp_host="0.0.0.0",
        )


def test_start_reports_missing_browser_executable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = DedicatedChromeLauncher(
        tmp_path / "profile",
        "https://www.doubao.com/chat/",
        executable=tmp_path / "missing.exe",
    )
    monkeypatch.setattr(launcher, "is_running", lambda: False)
    monkeypatch.setattr(launcher, "_cdp_endpoint_responds", lambda: False)

    with pytest.raises(DedicatedChromeUnavailable, match="浏览器"):
        launcher.start()


def test_start_times_out_without_terminating_spawned_browser(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "chrome.exe"
    executable.touch()
    launcher = DedicatedChromeLauncher(
        tmp_path / "profile",
        "https://www.doubao.com/chat/",
        executable=executable,
        startup_timeout_seconds=0,
    )
    monkeypatch.setattr(launcher, "is_running", lambda: False)
    monkeypatch.setattr(launcher, "_cdp_endpoint_responds", lambda: False)
    spawned: list[list[str]] = []
    monkeypatch.setattr(launcher, "_spawn", lambda command: spawned.append(command))

    with pytest.raises(DedicatedChromeUnavailable, match="连接端口"):
        launcher.start()

    assert len(spawned) == 1


def test_start_rejects_cdp_port_owned_by_another_browser(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "chrome.exe"
    executable.touch()
    launcher = DedicatedChromeLauncher(
        tmp_path / "profile",
        "https://www.doubao.com/chat/",
        executable=executable,
    )
    monkeypatch.setattr(launcher, "is_running", lambda: False)
    monkeypatch.setattr(launcher, "_cdp_endpoint_responds", lambda: True)
    spawned: list[list[str]] = []
    monkeypatch.setattr(launcher, "_spawn", lambda command: spawned.append(command))

    with pytest.raises(DedicatedChromeUnavailable, match="端口.*占用"):
        launcher.start()

    assert spawned == []


def test_is_running_accepts_a_live_cdp_page_before_url_navigation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response:
        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return b'[{"type":"page","url":""}]'

    launcher = DedicatedChromeLauncher(
        Path("profile"),
        "https://www.doubao.com/chat/",
    )
    monkeypatch.setattr("app.collection.dedicated_chrome.urlopen", lambda *_args, **_kwargs: Response())

    assert launcher.is_running() is True
