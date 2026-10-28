from __future__ import annotations

import re
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from queue import Queue
from threading import Event, Lock, Thread
from time import monotonic, sleep
from typing import Any, Protocol
from urllib.parse import quote_plus

from app.collection.doubao_web import (
    DoubaoWebCandidate,
    DoubaoWebContact,
    build_contact_prompt,
    build_discovery_prompt,
    parse_contact_response,
    parse_discovery_response,
)
from app.collection.prompt_pacing import PersistentPromptPacer


class DoubaoLoginRequired(RuntimeError):
    pass


class DoubaoChallengeRequired(RuntimeError):
    pass


class DoubaoWebUnavailable(RuntimeError):
    pass


class DoubaoWebTimeout(TimeoutError):
    pass


_CHALLENGE_URL_MARKERS = (
    "verifycenter",
    "verify.zijieapi.com",
    "captcha",
    "security-check",
    "challenge",
)
_CHALLENGE_TEXT_MARKERS = (
    "安全验证",
    "请完成验证",
    "图片验证",
    "拖动滑块",
    "滑动验证",
    "点选图中",
    "安全校验",
)


def _standalone_challenge_text(
    text: str,
    markers: tuple[str, ...] = _CHALLENGE_TEXT_MARKERS,
) -> bool:
    """Match a short, visible challenge label without scanning chat history."""
    for line in text.splitlines():
        normalized = re.sub(r"\s+", "", line).strip()
        if not normalized:
            continue
        if normalized in markers or (
            len(normalized) <= 24
            and any(normalized.startswith(marker) for marker in markers)
        ):
            return True
    return False


def _standalone_login_text(text: str) -> bool:
    lines = {
        re.sub(r"\s+", "", line).strip().strip("：:，,。！？!?")
        for line in text.splitlines()
        if line.strip()
    }
    return (
        {"登录", "有什么我能帮你的吗"} <= lines
        or bool(lines & {"登录以解锁更多功能", "扫码登录"})
    )


@dataclass(frozen=True, slots=True)
class DoubaoBrowserStatus:
    state: str
    detail: str
    current_company: str | None


class BrowserAdapter(Protocol):
    def start(self, *, cdp_url: str, chat_url: str) -> None: ...

    def visible_text(self) -> str: ...

    def challenge_detected(self) -> bool: ...

    def submit_prompt(
        self,
        prompt: str,
        *,
        timeout_seconds: int,
        cancel_event: Event | None = None,
    ) -> str: ...

    def search_public_web(
        self, query: str, *, max_results: int, timeout_seconds: int
    ) -> list[dict[str, str]]: ...

    def close(self) -> None: ...


def _is_response_update(
    current: str,
    baseline: str,
    submitted_prompt: str,
    *,
    current_message_count: int | None = None,
    baseline_message_count: int | None = None,
) -> bool:
    normalized = current.strip()
    text_changed = bool(
        normalized
        and normalized != baseline.strip()
        and re.sub(r"\s+", "", normalized)
        != re.sub(r"\s+", "", submitted_prompt.strip())
    )
    message_changed = bool(
        normalized
        and current_message_count is not None
        and baseline_message_count is not None
        and current_message_count > baseline_message_count
        and re.sub(r"\s+", "", normalized)
        != re.sub(r"\s+", "", submitted_prompt.strip())
    )
    return text_changed or message_changed


def installed_browser_channel() -> str:
    chrome = Path.home() / "AppData/Local/Google/Chrome/Application/chrome.exe"
    edge_paths = (
        Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
        Path("C:/Program Files/Microsoft/Edge/Application/msedge.exe"),
    )
    if chrome.exists():
        return "chrome"
    if any(path.exists() for path in edge_paths):
        return "msedge"
    raise DoubaoWebUnavailable("未找到可用的 Chrome 或 Edge 浏览器")


@dataclass(slots=True)
class _Command:
    name: str
    arguments: dict[str, Any]
    future: Future[Any]
    timeout_seconds: int
    cancel_event: Event


class DoubaoWebChatGateway:
    def __init__(
        self,
        *,
        cdp_url: str,
        chat_url: str = "https://www.doubao.com/chat/",
        request_timeout_seconds: int = 180,
        discovery_timeout_seconds: int | None = None,
        adapter_factory: Callable[[], BrowserAdapter] | None = None,
        request_interval_min_seconds: float = 0.0,
        request_interval_max_seconds: float = 0.0,
        batch_size: int | None = None,
        batch_size_min: int = 20,
        batch_size_max: int = 25,
        cooldown_min_seconds: float = 0.0,
        cooldown_max_seconds: float = 0.0,
        pacing_state_path: Path | None = None,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        self.cdp_url = cdp_url
        self.chat_url = chat_url
        self.request_timeout_seconds = request_timeout_seconds
        self.discovery_timeout_seconds = max(
            1,
            int(discovery_timeout_seconds or request_timeout_seconds),
        )
        self._adapter_factory = adapter_factory or PlaywrightDoubaoAdapter
        fixed_batch_size = max(1, int(batch_size)) if batch_size is not None else None
        self._prompt_pacer = PersistentPromptPacer(
            state_path=pacing_state_path,
            interval_min_seconds=request_interval_min_seconds,
            interval_max_seconds=request_interval_max_seconds,
            batch_size_min=fixed_batch_size or batch_size_min,
            batch_size_max=fixed_batch_size or batch_size_max,
            cooldown_min_seconds=cooldown_min_seconds,
            cooldown_max_seconds=cooldown_max_seconds,
            sleep_fn=sleep_fn or sleep,
        )
        self._commands: Queue[_Command | None] = Queue()
        self._thread: Thread | None = None
        self._lifecycle_lock = Lock()
        self._started = Event()
        self._closed = False
        self._startup_error: BaseException | None = None
        self._status = DoubaoBrowserStatus("stopped", "豆包网页浏览器未启动", None)

    def start(self) -> DoubaoBrowserStatus:
        with self._lifecycle_lock:
            if self._closed:
                raise DoubaoWebUnavailable("豆包网页浏览器已关闭")
            if self._thread is None:
                self._thread = Thread(target=self._actor_main, name="doubao-web-chat", daemon=True)
                self._thread.start()
        if not self._started.wait(timeout=min(self.request_timeout_seconds, 30)):
            raise DoubaoWebTimeout("启动豆包网页浏览器超时")
        if self._startup_error is not None:
            # The actor remains alive after a transient CDP failure. Reconnect
            # through that actor so a later start request can recover without
            # rebuilding the FastAPI process.
            try:
                return self.reconnect()
            except DoubaoWebUnavailable as exc:
                raise DoubaoWebUnavailable(str(self._startup_error)) from exc
        return self._submit("status")

    def status(self) -> DoubaoBrowserStatus:
        with self._lifecycle_lock:
            if self._closed:
                raise DoubaoWebUnavailable("豆包网页浏览器已关闭")
            # Status is polled frequently by the UI. Never enqueue a command
            # behind a slow CDP operation: the cached state is updated at each
            # command boundary, while refresh_status is available when a fresh
            # page inspection is explicitly required.
            status = self._status
            if status.state == "working" and status.detail == "等待豆包网页查询间隔":
                remaining = self._prompt_pacer.seconds_until_slot()
                if remaining > 0:
                    minutes = max(1, int((remaining + 59) // 60))
                    return DoubaoBrowserStatus(
                        "working",
                        f"等待豆包网页查询间隔，剩余约 {minutes} 分钟",
                        status.current_company,
                    )
            return status

    def refresh_status(self) -> DoubaoBrowserStatus:
        return self._submit("refresh_status")

    def reconnect(self) -> DoubaoBrowserStatus:
        return self._submit("reconnect")

    def discover_companies(
        self, *, city: str, keywords: list[str], request_text: str
    ) -> list[DoubaoWebCandidate]:
        prompt = build_discovery_prompt(city=city, keywords=keywords, request_text=request_text)
        text = self._submit(
            "prompt",
            prompt=prompt,
            current_company=None,
            timeout_seconds=self.discovery_timeout_seconds,
        )
        return parse_discovery_response(text, city=city, keywords=keywords)

    def enrich_company(
        self,
        *,
        company_name: str,
        city: str,
        requested_contacts: list[str],
    ) -> DoubaoWebContact:
        prompt = build_contact_prompt(
            company_name=company_name,
            city=city,
            requested_contacts=requested_contacts,
        )
        text = self._submit("prompt", prompt=prompt, current_company=company_name)
        return parse_contact_response(text, company_name=company_name)

    def search_public_web(
        self, *, query: str, max_results: int = 10
    ) -> list[dict[str, str]]:
        return self._submit("search", query=query, max_results=max_results)

    def shutdown(self) -> None:
        with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            thread = self._thread
            if thread is None:
                return
            self._commands.put(None)
        thread.join(timeout=10)

    def _submit(
        self,
        name: str,
        *,
        timeout_seconds: int | None = None,
        **arguments: Any,
    ) -> Any:
        command_timeout = max(1, int(timeout_seconds or self.request_timeout_seconds))
        pacing_wait = (
            max(0.0, float(self._prompt_pacer.seconds_until_slot()))
            if name == "prompt"
            else 0.0
        )
        should_start = False
        with self._lifecycle_lock:
            if self._closed:
                raise DoubaoWebUnavailable("豆包网页浏览器已关闭")
            thread = self._thread
            if thread is None:
                should_start = True
            elif not thread.is_alive():
                raise DoubaoWebUnavailable("豆包网页浏览器未启动")
        if should_start:
            self.start()
            return self._submit(name, timeout_seconds=command_timeout, **arguments)
        with self._lifecycle_lock:
            future: Future[Any] = Future()
            command = _Command(
                name,
                arguments,
                future,
                command_timeout,
                Event(),
            )
            self._commands.put(command)
        try:
            return future.result(timeout=command_timeout + pacing_wait + 10)
        except TimeoutError as exc:
            command.cancel_event.set()
            raise DoubaoWebTimeout("豆包网页操作超时") from exc

    def _actor_main(self) -> None:
        adapter: BrowserAdapter | None = None
        try:
            adapter = self._adapter_factory()
            self._status = DoubaoBrowserStatus("connecting", "正在连接豆包专用浏览器", None)
            adapter.start(cdp_url=self.cdp_url, chat_url=self.chat_url)
            try:
                self._guard_page(adapter.visible_text(), None, self._challenge_detected(adapter))
            except (DoubaoLoginRequired, DoubaoChallengeRequired):
                pass
            except Exception as exc:
                if not self._is_closed_browser_error(exc):
                    raise
                self._disconnect_adapter(adapter)
                self._status = DoubaoBrowserStatus("reconnecting", "豆包连接中断，正在恢复", None)
                adapter = self._connect_with_retries()
                try:
                    self._guard_page(adapter.visible_text(), None, self._challenge_detected(adapter))
                except (DoubaoLoginRequired, DoubaoChallengeRequired):
                    pass
                else:
                    self._status = DoubaoBrowserStatus("ready", "豆包网页已重新连接", None)
            else:
                self._status = DoubaoBrowserStatus("ready", "豆包网页已就绪", None)
        except Exception as exc:  # noqa: BLE001 - translate adapter startup at the actor boundary
            self._startup_error = exc
            self._status = DoubaoBrowserStatus("unavailable", str(exc), None)
        finally:
            self._started.set()

        try:
            while command := self._commands.get():
                if command.name == "reconnect":
                    if adapter is not None:
                        self._disconnect_adapter(adapter)
                    adapter = None
                    self._status = DoubaoBrowserStatus("reconnecting", "正在重新连接豆包专用浏览器", None)
                    try:
                        adapter = self._connect_with_retries()
                        self._startup_error = None
                        try:
                            self._guard_page(adapter.visible_text(), None, self._challenge_detected(adapter))
                        except (DoubaoLoginRequired, DoubaoChallengeRequired):
                            pass
                        else:
                            self._status = DoubaoBrowserStatus("ready", "豆包网页已重新连接", None)
                        command.future.set_result(self._status)
                    except Exception as reconnect_error:  # noqa: BLE001 - complete the command
                        self._status = DoubaoBrowserStatus("unavailable", str(reconnect_error), None)
                        command.future.set_exception(DoubaoWebUnavailable(str(reconnect_error)))
                    continue
                if adapter is None:
                    command.future.set_exception(DoubaoWebUnavailable(self._status.detail))
                    continue
                try:
                    command.future.set_result(self._execute(adapter, command))
                except Exception as exc:  # noqa: BLE001 - every command error must complete its future
                    closed_browser = self._is_closed_browser_error(exc)
                    if not closed_browser:
                        command.future.set_exception(exc)
                        continue
                    try:
                        self._disconnect_adapter(adapter)
                    except Exception:  # noqa: BLE001, S110 - the client is already disconnected
                        pass
                    try:
                        self._status = DoubaoBrowserStatus("reconnecting", "豆包连接中断，正在恢复", None)
                        adapter = self._connect_with_retries()
                        self._status = DoubaoBrowserStatus("ready", "豆包网页已重新连接", None)
                        command.future.set_result(self._execute(adapter, command))
                    except Exception as recovery_error:  # noqa: BLE001 - complete the command
                        self._status = DoubaoBrowserStatus(
                            "unavailable", str(recovery_error), None
                        )
                        command.future.set_exception(recovery_error)
        finally:
            if adapter is not None:
                self._disconnect_adapter(adapter)
            self._status = DoubaoBrowserStatus("stopped", "后端连接已断开，专用浏览器保持运行", None)

    def _connect_with_retries(self) -> BrowserAdapter:
        last_error: Exception | None = None
        for delay in (0.25, 0.5, 1.0):
            candidate = self._adapter_factory()
            try:
                candidate.start(cdp_url=self.cdp_url, chat_url=self.chat_url)
                return candidate
            except Exception as exc:  # noqa: BLE001 - retry transient CDP failures
                last_error = exc
                self._disconnect_adapter(candidate)
                sleep(delay)
        assert last_error is not None
        raise last_error

    @staticmethod
    def _disconnect_adapter(adapter: BrowserAdapter) -> None:
        adapter.close()

    @staticmethod
    def _is_closed_browser_error(exc: BaseException) -> bool:
        message = str(exc).lower()
        return type(exc).__name__ == "TargetClosedError" or any(
            marker in message
            for marker in (
                "target page, context or browser has been closed",
                "browser has been closed",
                "connection closed",
            )
        )

    def _execute(self, adapter: BrowserAdapter, command: _Command) -> Any:
        if command.name == "status":
            if self._status.state != "working":
                try:
                    self._guard_page(
                        adapter.visible_text(),
                        self._status.current_company,
                        self._challenge_detected(adapter),
                    )
                except (DoubaoLoginRequired, DoubaoChallengeRequired):
                    return self._status
                self._status = DoubaoBrowserStatus("ready", "豆包网页已就绪", None)
            return self._status
        if command.name == "refresh_status":
            try:
                self._guard_page(
                    adapter.visible_text(),
                    self._status.current_company,
                    self._challenge_detected(adapter),
                )
            except (DoubaoLoginRequired, DoubaoChallengeRequired):
                return self._status
            self._status = DoubaoBrowserStatus("ready", "豆包网页已就绪", None)
            return self._status
        if command.name == "search":
            self._status = DoubaoBrowserStatus("working", "正在查询公开网页", None)
            try:
                results = adapter.search_public_web(
                    str(command.arguments["query"]),
                    max_results=int(command.arguments.get("max_results") or 10),
                    timeout_seconds=min(command.timeout_seconds, 20),
                )
            except TimeoutError as exc:
                self._status = DoubaoBrowserStatus("timeout", str(exc), None)
                raise DoubaoWebTimeout(str(exc)) from exc
            except Exception as exc:
                self._status = DoubaoBrowserStatus("unavailable", str(exc), None)
                raise DoubaoWebUnavailable(str(exc)) from exc
            self._status = DoubaoBrowserStatus("ready", "公开网页查询完成", None)
            return results
        if command.name != "prompt":
            raise DoubaoWebUnavailable(f"不支持的豆包网页命令: {command.name}")
        company = command.arguments.get("current_company")
        self._guard_page(adapter.visible_text(), company, self._challenge_detected(adapter))
        self._status = DoubaoBrowserStatus("working", "等待豆包网页查询间隔", company)
        if not self._prompt_pacer.wait_for_slot(cancel_event=command.cancel_event):
            raise DoubaoWebTimeout("豆包网页查询间隔等待已取消")
        self._prompt_pacer.mark_submitted()
        self._status = DoubaoBrowserStatus("working", "正在查询豆包网页", company)
        try:
            response = adapter.submit_prompt(
                str(command.arguments["prompt"]),
                timeout_seconds=command.timeout_seconds,
                cancel_event=command.cancel_event,
            )
            self._guard_page(adapter.visible_text(), company, self._challenge_detected(adapter))
        except (DoubaoLoginRequired, DoubaoChallengeRequired):
            raise
        except TimeoutError as exc:
            if self._challenge_detected(adapter):
                detail = "豆包网页需要完成安全验证"
                self._status = DoubaoBrowserStatus("challenge", detail, company)
                raise DoubaoChallengeRequired(detail) from exc
            self._status = DoubaoBrowserStatus("timeout", str(exc), company)
            raise DoubaoWebTimeout(str(exc)) from exc
        except Exception as exc:
            if self._challenge_detected(adapter):
                detail = "豆包网页需要完成安全验证"
                self._status = DoubaoBrowserStatus("challenge", detail, company)
                raise DoubaoChallengeRequired(detail) from exc
            self._status = DoubaoBrowserStatus("unavailable", str(exc), company)
            raise DoubaoWebUnavailable(str(exc)) from exc
        self._status = DoubaoBrowserStatus("ready", "豆包网页已就绪", None)
        return response

    @staticmethod
    def _challenge_detected(adapter: BrowserAdapter) -> bool:
        detector = getattr(adapter, "challenge_detected", None)
        if not callable(detector):
            return False
        try:
            return bool(detector())
        except Exception:  # noqa: BLE001 - status checks must not break the actor
            return False

    def _guard_page(
        self,
        visible_text: str,
        company: str | None,
        challenge_detected: bool = False,
    ) -> None:
        if challenge_detected:
            detail = "豆包网页需要完成安全验证"
            self._status = DoubaoBrowserStatus("challenge", detail, company)
            raise DoubaoChallengeRequired(detail)
        if _standalone_login_text(visible_text):
            detail = "需要登录豆包网页"
            self._status = DoubaoBrowserStatus("waiting_for_login", detail, company)
            raise DoubaoLoginRequired(detail)


class PlaywrightDoubaoAdapter:
    _PLACEHOLDERS = ("给豆包发送消息", "发消息", "请输入你的问题")
    # Search-backed replies can spend a while fetching sources before the first
    # message node appears. Keep the overall request timeout bounded, but allow
    # a slower first response before declaring the page dead.
    _RESPONSE_START_GRACE_SECONDS = 60

    def __init__(self) -> None:
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._chat_url = "https://www.doubao.com/chat/"

    def start(self, *, cdp_url: str, chat_url: str) -> None:
        from playwright.sync_api import sync_playwright

        self._chat_url = chat_url
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.connect_over_cdp(cdp_url)
        if not self._browser.contexts:
            raise DoubaoWebUnavailable("专用浏览器没有可用的浏览器上下文")
        self._context = self._browser.contexts[0]
        self._page = next(
            (page for page in self._context.pages if "doubao.com/chat" in page.url),
            None,
        )
        if self._page is None:
            self._page = self._context.new_page()
            self._page.goto(chat_url, wait_until="domcontentloaded")

    def _ensure_page(self) -> Any:
        if self._page is not None:
            try:
                is_closed = getattr(self._page, "is_closed", None)
                if is_closed is None or not is_closed():
                    return self._page
            except Exception:  # noqa: BLE001 - reconnect against a stale CDP page
                pass
        if self._context is None:
            raise DoubaoWebUnavailable("豆包网页浏览器未连接")
        self._page = next(
            (page for page in self._context.pages if "doubao.com/chat" in page.url),
            None,
        )
        if self._page is None:
            self._page = self._context.new_page()
            self._page.goto(self._chat_url, wait_until="domcontentloaded")
        return self._page

    def visible_text(self) -> str:
        return str(self._ensure_page().locator("body").inner_text(timeout=10_000))

    def challenge_detected(self) -> bool:
        """Detect an official verification frame without interacting with it."""
        page = self._ensure_page()
        try:
            page_url = str(getattr(page, "url", "") or "").lower()
            if any(marker in page_url for marker in _CHALLENGE_URL_MARKERS):
                return True
            verification_iframes = page.locator(
                "iframe[src*='verifycenter'], iframe[src*='captcha'], "
                "iframe[src*='verify.zijieapi.com'], iframe[title*='verify']"
            )
            if self._has_visible_locator(verification_iframes):
                return True
            image_challenge_markers = page.locator(
                "[class*='captcha' i], [id*='captcha' i], "
                "[data-testid*='captcha' i], [class*='verify-center' i], "
                "[id*='verify-center' i], [aria-label*='验证码' i], "
                "[aria-label*='安全验证' i], [role='dialog'] img[src*='verify' i], "
                "[role='dialog'] img[src*='captcha' i]"
            )
            # Doubao keeps some captcha-related support nodes in the DOM after
            # the dialog closes. Only a visible marker represents an active
            # challenge; hidden nodes must not leave a signed-in chat paused.
            if self._has_visible_locator(image_challenge_markers):
                return True
            for frame in page.frames:
                frame_url = str(getattr(frame, "url", "") or "").lower()
                if any(marker in frame_url for marker in _CHALLENGE_URL_MARKERS):
                    try:
                        if frame == page.main_frame or not frame.frame_element().is_visible():
                            continue
                    except Exception:  # noqa: BLE001, S112 - detached/hidden frames are not active challenges
                        continue
                    return True
                if frame == page.main_frame:
                    continue
                try:
                    if not frame.frame_element().is_visible():
                        continue
                    frame_text = frame.locator("body").inner_text(timeout=1_000)
                except Exception:  # noqa: BLE001 - a detached frame is not a challenge signal
                    frame_text = ""
                if _standalone_challenge_text(
                    frame_text,
                    (*_CHALLENGE_TEXT_MARKERS, "验证码"),
                ):
                    return True
        except Exception:  # noqa: BLE001 - status checks must remain non-invasive
            return False
        return False

    @staticmethod
    def _has_visible_locator(locator: Any) -> bool:
        count = locator.count()
        for index in range(count):
            candidate = locator.nth(index) if hasattr(locator, "nth") else locator
            try:
                if candidate.is_visible():
                    return True
            except Exception:  # noqa: BLE001, S112 - stale DOM nodes are not challenge signals
                continue
        return False

    def submit_prompt(
        self,
        prompt: str,
        *,
        timeout_seconds: int,
        cancel_event: Event | None = None,
    ) -> str:
        editor = self._find_editor()
        baseline = self._latest_response_text()
        baseline_message_count = self._latest_response_message_count()
        editor.fill(prompt)
        editor.press("Enter")
        deadline = monotonic() + timeout_seconds
        response_start_deadline = monotonic() + min(
            timeout_seconds,
            self._RESPONSE_START_GRACE_SECONDS,
        )
        previous = baseline
        response_started = False
        stable_polls = 0
        while (now := monotonic()) < deadline:
            if cancel_event is not None and cancel_event.is_set():
                raise DoubaoWebTimeout("豆包网页操作已取消")
            current = self._latest_response_text()
            current_message_count = self._latest_response_message_count()
            generating = self._is_generating()
            if _is_response_update(
                current,
                baseline,
                prompt,
                current_message_count=current_message_count,
                baseline_message_count=baseline_message_count,
            ):
                response_started = True
            if not response_started and not generating and now >= response_start_deadline:
                raise DoubaoWebTimeout("等待豆包网页开始返回超时")
            stable_polls = (
                stable_polls + 1
                if response_started and current and current == previous
                else 0
            )
            if response_started and stable_polls >= 8 and not generating:
                return current
            previous = current
            if cancel_event is not None:
                if cancel_event.wait(1):
                    raise DoubaoWebTimeout("豆包网页操作已取消")
            else:
                sleep(1)
        raise DoubaoWebTimeout("等待豆包网页回答超时")

    def search_public_web(
        self, query: str, *, max_results: int, timeout_seconds: int
    ) -> list[dict[str, str]]:
        search_page = self._context.new_page()
        try:
            search_page.goto(
                "https://www.baidu.com/s?wd=" + quote_plus(query),
                wait_until="domcontentloaded",
                timeout=timeout_seconds * 1000,
            )
            search_page.wait_for_selector(
                "#content_left", timeout=min(timeout_seconds * 1000, 10_000)
            )
            items = search_page.locator("#content_left .result")
            results: list[dict[str, str]] = []
            for index in range(min(items.count(), max_results)):
                item = items.nth(index)
                anchor = item.locator("h3 a")
                if not anchor.count():
                    continue
                title = str(anchor.first.text_content() or "").strip()
                url = str(anchor.first.get_attribute("href") or "").strip()
                text = str(item.text_content() or "").strip()
                if not title or not url or not text:
                    continue
                results.append({"title": title, "url": url, "text": text})
            return results
        finally:
            search_page.close()

    def close(self) -> None:
        if self._playwright is not None:
            self._playwright.stop()
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

    def _find_editor(self) -> Any:
        page = self._ensure_page()
        for placeholder in self._PLACEHOLDERS:
            locator = page.get_by_placeholder(placeholder, exact=False)
            if locator.count() == 1:
                return locator
        textareas = page.locator("textarea")
        if textareas.count() == 1:
            return textareas
        raise DoubaoWebUnavailable("找不到豆包网页输入框")

    def _latest_response_text(self) -> str:
        page = self._ensure_page()
        for selector in (
            ".md-box-root",
            ".flow-markdown-body",
            "[data-testid='message']",
        ):
            messages = page.locator(selector)
            count = messages.count()
            if count:
                return str(messages.nth(count - 1).inner_text()).strip()
        return self.visible_text()

    def _latest_response_message_count(self) -> int:
        try:
            page = self._ensure_page()
        except DoubaoWebUnavailable:
            # Test doubles and transiently detached pages may only expose text.
            # Preserve the text-based response detection in that case.
            return 0
        for selector in (
            ".md-box-root",
            ".flow-markdown-body",
            "[data-testid='message']",
        ):
            messages = page.locator(selector)
            count = messages.count()
            if count:
                return count
        return 0

    def _is_generating(self) -> bool:
        page = self._ensure_page()
        for label in ("停止生成", "停止回答"):
            buttons = page.get_by_role("button", name=label, exact=False)
            if buttons.count() and buttons.first.is_visible():
                return True
        return False
