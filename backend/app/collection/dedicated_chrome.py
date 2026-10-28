from __future__ import annotations

import json
import subprocess
from pathlib import Path
from time import monotonic, sleep
from urllib.request import urlopen


class DedicatedChromeUnavailable(RuntimeError):
    pass


def installed_browser_executable() -> Path:
    candidates = (
        Path.home() / "AppData/Local/Google/Chrome/Application/chrome.exe",
        Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
        Path("C:/Program Files (x86)/Google/Chrome/Application/chrome.exe"),
        Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
        Path("C:/Program Files/Microsoft/Edge/Application/msedge.exe"),
    )
    if executable := next((path for path in candidates if path.exists()), None):
        return executable
    raise DedicatedChromeUnavailable("未找到可用的 Chrome 或 Edge 浏览器")


class DedicatedChromeLauncher:
    _MARKER_URL = "data:text/plain,doubao-dedicated-browser"

    def __init__(
        self,
        profile_dir: Path,
        chat_url: str,
        *,
        cdp_host: str = "127.0.0.1",
        cdp_port: int = 9333,
        executable: Path | None = None,
        startup_timeout_seconds: float = 10,
    ) -> None:
        if cdp_host != "127.0.0.1":
            raise ValueError("豆包专用浏览器 CDP 地址必须为 127.0.0.1")
        self.profile_dir = Path(profile_dir)
        self.chat_url = chat_url
        self.cdp_host = cdp_host
        self.cdp_port = cdp_port
        self.executable = Path(executable) if executable else None
        self.startup_timeout_seconds = startup_timeout_seconds

    @property
    def cdp_url(self) -> str:
        return f"http://{self.cdp_host}:{self.cdp_port}"

    def is_running(self) -> bool:
        try:
            with urlopen(f"{self.cdp_url}/json/list", timeout=1) as response:
                targets = json.loads(response.read().decode("utf-8"))
            return any(
                self._MARKER_URL in str(target.get("url") or "")
                or "doubao.com/chat" in str(target.get("url") or "")
                for target in targets
            ) or any(target.get("type") == "page" for target in targets)
        except (json.JSONDecodeError, OSError, TimeoutError, TypeError, ValueError):
            return False

    def _cdp_endpoint_responds(self) -> bool:
        try:
            with urlopen(f"{self.cdp_url}/json/version", timeout=1) as response:
                return response.status == 200
        except (OSError, TimeoutError, ValueError):
            return False

    def start(self) -> bool:
        if self.is_running():
            return False
        if self._cdp_endpoint_responds():
            raise DedicatedChromeUnavailable(
                f"本机 CDP 端口 {self.cdp_port} 已被其他浏览器占用"
            )
        executable = self.executable or installed_browser_executable()
        if not executable.is_file():
            raise DedicatedChromeUnavailable(f"浏览器程序不存在: {executable}")
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._spawn(
            [
                str(executable),
                f"--remote-debugging-address={self.cdp_host}",
                f"--remote-debugging-port={self.cdp_port}",
                f"--user-data-dir={self.profile_dir}",
                "--no-first-run",
                # The local Chromium GPU process crashes on this machine;
                # force software rendering so the CDP window stays alive.
                "--disable-gpu",
                "--disable-gpu-compositing",
                "--disable-gpu-rasterization",
                "--disable-gpu-sandbox",
                "--in-process-gpu",
                "--use-gl=swiftshader",
                self._MARKER_URL,
                self.chat_url,
            ]
        )
        deadline = monotonic() + self.startup_timeout_seconds
        while True:
            if self.is_running():
                return True
            if monotonic() >= deadline:
                raise DedicatedChromeUnavailable("专用浏览器已启动，但无法连接本机 CDP 连接端口")
            sleep(0.1)

    def _spawn(self, command: list[str]) -> None:
        subprocess.Popen(
            command,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
            close_fds=True,
        )
