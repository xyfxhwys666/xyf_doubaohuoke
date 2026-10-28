from pathlib import Path
from threading import Event, Thread, get_ident
from time import monotonic
from types import SimpleNamespace

import pytest

from app.collection import doubao_browser
from app.collection.doubao_browser import (
    DoubaoChallengeRequired,
    DoubaoLoginRequired,
    DoubaoWebChatGateway,
    DoubaoWebTimeout,
    DoubaoWebUnavailable,
    PlaywrightDoubaoAdapter,
    installed_browser_channel,
)


class FakeAdapter:
    def __init__(self, responses: list[str], *, visible_text: str = "") -> None:
        self.responses = list(responses)
        self.page_text = visible_text
        self.thread_ids: set[int] = set()
        self.owner_thread_id: int | None = None
        self.cdp_url: str | None = None
        self.prompts: list[str] = []
        self.searches: list[str] = []
        self.search_timeouts: list[int] = []
        self.closed = False

    def start(self, *, cdp_url: str, chat_url: str) -> None:
        del chat_url
        self._record()
        self.owner_thread_id = get_ident()
        self.cdp_url = cdp_url

    def visible_text(self) -> str:
        self._record()
        return self.page_text

    def submit_prompt(
        self,
        prompt: str,
        *,
        timeout_seconds: int,
        cancel_event: Event | None = None,
    ) -> str:
        del timeout_seconds
        del cancel_event
        self._record()
        self.prompts.append(prompt)
        return self.responses.pop(0)

    def search_public_web(
        self, query: str, *, max_results: int, timeout_seconds: int
    ) -> list[dict[str, str]]:
        del max_results
        self._record()
        self.searches.append(query)
        self.search_timeouts.append(timeout_seconds)
        return [{
            "title": "深圳市测试设计有限公司",
            "url": "http://www.baidu.com/link?url=test",
            "text": "深圳市测试设计有限公司 深圳别墅设计 13800138000",
        }]

    def close(self) -> None:
        self._record()
        self.closed = True

    def _record(self) -> None:
        self.thread_ids.add(get_ident())


class ClosedPageAdapter(FakeAdapter):
    def visible_text(self) -> str:
        self._record()
        raise RuntimeError("Target page, context or browser has been closed")


class RecordingContext:
    def __init__(self, pages: list["RecordingPage"] | None = None) -> None:
        self.pages = pages or []
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1

    def new_page(self) -> "RecordingPage":
        page = RecordingPage("about:blank")
        self.pages.append(page)
        return page


class RecordingPage:
    def __init__(self, url: str) -> None:
        self.url = url
        self.goto_calls: list[str] = []

    def goto(self, url: str, *, wait_until: str) -> None:
        del wait_until
        self.goto_calls.append(url)
        self.url = url


class RecordingBrowser:
    def __init__(self, context: RecordingContext) -> None:
        self.contexts = [context]
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class RecordingChromium:
    def __init__(self, browser: RecordingBrowser) -> None:
        self.browser = browser
        self.cdp_urls: list[str] = []

    def connect_over_cdp(self, cdp_url: str) -> RecordingBrowser:
        self.cdp_urls.append(cdp_url)
        return self.browser


class RecordingPlaywright:
    def __init__(self, browser: RecordingBrowser | None = None) -> None:
        self.stopped = False
        self.chromium = RecordingChromium(browser) if browser else None

    def stop(self) -> None:
        self.stopped = True


class RecordingPlaywrightStarter:
    def __init__(self, playwright: RecordingPlaywright) -> None:
        self.playwright = playwright

    def start(self) -> RecordingPlaywright:
        return self.playwright


class ResponseLocator:
    def __init__(self, messages: list[str]) -> None:
        self.messages = messages

    def count(self) -> int:
        return len(self.messages)

    def nth(self, index: int) -> "ResponseLocator":
        return ResponseLocator([self.messages[index]])

    def inner_text(self) -> str:
        return self.messages[0]


class CurrentDoubaoPage:
    def __init__(self) -> None:
        self.selectors: list[str] = []

    def locator(self, selector: str) -> ResponseLocator:
        self.selectors.append(selector)
        if selector == ".md-box-root":
            return ResponseLocator(["old response", '[{"company_name":"深圳测试公司"}]'])
        return ResponseLocator([])


def test_adapter_rebinds_existing_doubao_page_when_page_reference_is_missing() -> None:
    doubao_page = CurrentDoubaoPage()
    doubao_page.url = "https://www.doubao.com/chat/38436254489424898"
    context = RecordingContext([doubao_page])
    adapter = PlaywrightDoubaoAdapter()
    adapter._context = context
    adapter._chat_url = "https://www.doubao.com/chat/"
    adapter._page = None

    assert adapter._ensure_page() is doubao_page


def test_gateway_owns_adapter_on_one_thread_and_parses_responses(tmp_path: Path) -> None:
    adapter = FakeAdapter(
        ["1｜深圳市测试设计有限公司｜https://example.cn/about｜主营别墅全案"]
    )
    gateway = DoubaoWebChatGateway(cdp_url="http://127.0.0.1:9223", adapter_factory=lambda: adapter)
    try:
        gateway.start()
        result = gateway.discover_companies(
            city="深圳市",
            keywords=["别墅设计"],
            request_text="查找别墅全案公司",
        )

        assert gateway.status().state == "ready"
        assert adapter.owner_thread_id is not None
        assert adapter.thread_ids == {adapter.owner_thread_id}
        assert adapter.cdp_url == "http://127.0.0.1:9223"
        assert result[0].company_name == "深圳市测试设计有限公司"
    finally:
        gateway.shutdown()


def test_gateway_autostarts_for_first_prompt_when_cdp_is_available() -> None:
    adapter = FakeAdapter(
        ["1｜深圳市测试设计有限公司｜https://example.cn/about｜主营别墅全案"]
    )
    gateway = DoubaoWebChatGateway(
        cdp_url="http://127.0.0.1:9223",
        adapter_factory=lambda: adapter,
    )
    try:
        result = gateway.discover_companies(
            city="深圳市",
            keywords=["软装"],
            request_text="找深圳软装公司",
        )
    finally:
        gateway.shutdown()

    assert result[0].company_name == "深圳市测试设计有限公司"


def test_gateway_uses_discovery_timeout_for_discovery_prompts() -> None:
    class TimeoutRecordingAdapter(FakeAdapter):
        def __init__(self) -> None:
            super().__init__(
                ["1｜深圳市测试设计有限公司｜https://example.cn/about｜主营别墅全案"]
            )
            self.prompt_timeouts: list[int] = []

        def submit_prompt(
            self,
            prompt: str,
            *,
            timeout_seconds: int,
            cancel_event: Event | None = None,
        ) -> str:
            self.prompt_timeouts.append(timeout_seconds)
            return super().submit_prompt(
                prompt,
                timeout_seconds=timeout_seconds,
                cancel_event=cancel_event,
            )

    adapter = TimeoutRecordingAdapter()
    gateway = DoubaoWebChatGateway(
        cdp_url="http://127.0.0.1:9223",
        request_timeout_seconds=30,
        discovery_timeout_seconds=7,
        adapter_factory=lambda: adapter,
    )
    try:
        gateway.discover_companies(
            city="深圳市",
            keywords=["别墅设计"],
            request_text="查找别墅全案公司",
        )
    finally:
        gateway.shutdown()

    assert adapter.prompt_timeouts == [7]


def test_gateway_runs_public_search_on_same_browser_actor_thread(tmp_path: Path) -> None:
    adapter = FakeAdapter([])
    gateway = DoubaoWebChatGateway(
        cdp_url="http://127.0.0.1:9223", adapter_factory=lambda: adapter
    )
    try:
        gateway.start()
        results = gateway.search_public_web(
            query="深圳市测试设计有限公司 13800138000"
        )

        assert results[0]["title"] == "深圳市测试设计有限公司"
        assert adapter.searches == ["深圳市测试设计有限公司 13800138000"]
        assert adapter.search_timeouts == [20]
        assert adapter.thread_ids == {adapter.owner_thread_id}
    finally:
        gateway.shutdown()


def test_gateway_reopens_closed_browser_and_retries_prompt(tmp_path: Path) -> None:
    closed = ClosedPageAdapter([])
    recovered = FakeAdapter(
        ["1｜深圳市测试设计有限公司｜https://example.cn/about｜主营别墅全案"]
    )
    adapters = iter([closed, recovered])
    gateway = DoubaoWebChatGateway(
        cdp_url="http://127.0.0.1:9223",
        adapter_factory=lambda: next(adapters),
    )
    try:
        gateway.start()

        result = gateway.discover_companies(
            city="深圳市",
            keywords=["别墅设计"],
            request_text="查找别墅全案公司",
        )

        assert closed.closed is True
        assert result[0].company_name == "深圳市测试设计有限公司"
        assert gateway.status().state == "ready"
    finally:
        gateway.shutdown()


def test_gateway_start_recovers_after_stale_initial_cdp_connection() -> None:
    class FailingStartAdapter(FakeAdapter):
        def start(self, *, cdp_url: str, chat_url: str) -> None:
            del cdp_url, chat_url
            raise RuntimeError("stale CDP connection")

    adapters = iter([FailingStartAdapter([]), FakeAdapter([])])
    gateway = DoubaoWebChatGateway(
        cdp_url="http://127.0.0.1:9333",
        request_timeout_seconds=2,
        adapter_factory=lambda: next(adapters),
    )
    try:
        assert gateway.start().state == "ready"
    finally:
        gateway.shutdown()


def test_adapter_connects_to_existing_browser_and_reuses_doubao_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    doubao_page = RecordingPage("https://www.doubao.com/chat/123")
    context = RecordingContext([RecordingPage("https://example.test/"), doubao_page])
    browser = RecordingBrowser(context)
    playwright = RecordingPlaywright(browser)
    monkeypatch.setattr(
        "playwright.sync_api.sync_playwright",
        lambda: RecordingPlaywrightStarter(playwright),
    )
    adapter = PlaywrightDoubaoAdapter()

    adapter.start(
        cdp_url="http://127.0.0.1:9223",
        chat_url="https://www.doubao.com/chat/",
    )

    assert playwright.chromium.cdp_urls == ["http://127.0.0.1:9223"]
    assert adapter._page is doubao_page
    assert doubao_page.goto_calls == []


def test_adapter_close_disconnects_without_closing_remote_context() -> None:
    adapter = PlaywrightDoubaoAdapter()
    context = RecordingContext()
    browser = RecordingBrowser(context)
    playwright = RecordingPlaywright()
    adapter._browser = browser
    adapter._context = context
    adapter._playwright = playwright

    adapter.close()

    assert browser.close_calls == 0
    assert context.close_calls == 0
    assert playwright.stopped is True


def test_adapter_reads_latest_response_from_current_doubao_message_container() -> None:
    adapter = PlaywrightDoubaoAdapter()
    page = CurrentDoubaoPage()
    adapter._page = page

    assert adapter._latest_response_text() == '[{"company_name":"深圳测试公司"}]'
    assert page.selectors[0] == ".md-box-root"


def test_submitted_prompt_is_not_treated_as_a_new_doubao_response() -> None:
    prompt = "请联网查询深圳测试公司"
    displayed_prompt = "请联网查询 深圳 测试公司"
    baseline = '[{"company_name":"上一家公司"}]'

    assert doubao_browser._is_response_update(displayed_prompt, baseline, prompt) is False
    assert (
        doubao_browser._is_response_update(
            '[{"company_name":"深圳测试公司"}]',
            baseline,
            prompt,
        )
        is True
    )


def test_identical_text_from_a_new_message_is_treated_as_a_new_response() -> None:
    assert (
        doubao_browser._is_response_update(
            "未找到可核验公开手机号",
            "未找到可核验公开手机号",
            "请联网查询深圳方物空间设计事务所",
            current_message_count=10,
            baseline_message_count=9,
        )
        is True
    )


def test_submit_prompt_accepts_identical_text_from_a_new_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Editor:
        def fill(self, _prompt: str) -> None:
            pass

        def press(self, _key: str) -> None:
            pass

    class IdenticalResponseAdapter(PlaywrightDoubaoAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.message_count_calls = 0

        def _find_editor(self) -> Editor:
            return Editor()

        def _latest_response_text(self) -> str:
            return "未找到可核验公开手机号"

        def _latest_response_message_count(self) -> int:
            self.message_count_calls += 1
            return 1 if self.message_count_calls == 1 else 2

        def _is_generating(self) -> bool:
            return False

    monkeypatch.setattr("app.collection.doubao_browser.sleep", lambda _seconds: None)

    assert (
        IdenticalResponseAdapter().submit_prompt(
            "请联网查询深圳方物空间设计事务所",
            timeout_seconds=180,
        )
        == "未找到可核验公开手机号"
    )


def test_browser_adapter_stops_waiting_when_command_is_cancelled() -> None:
    cancel_event = Event()

    class StalledAdapter(PlaywrightDoubaoAdapter):
        def _find_editor(self) -> object:
            return type(
                "Editor",
                (),
                {"fill": lambda _self, _prompt: None, "press": lambda _self, _key: None},
            )()

        def _latest_response_text(self) -> str:
            return "旧回答"

        def _latest_response_message_count(self) -> int:
            return 1

        def _is_generating(self) -> bool:
            return True

    worker_error: dict[str, BaseException] = {}

    def run_prompt() -> None:
        try:
            StalledAdapter().submit_prompt(
                "新问题",
                timeout_seconds=180,
                cancel_event=cancel_event,
            )
        except DoubaoWebTimeout as exc:  # pragma: no cover - asserted below
            worker_error["error"] = exc

    worker = Thread(target=run_prompt, daemon=True)
    worker.start()
    cancel_event.set()
    worker.join(timeout=1)

    assert not worker.is_alive()
    assert isinstance(worker_error.get("error"), DoubaoWebTimeout)
    assert "取消" in str(worker_error["error"])


def test_prompt_command_timeout_includes_pacing_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_timeouts: list[float] = []

    class RecordingFuture:
        def result(self, timeout: float) -> str:
            observed_timeouts.append(timeout)
            return "ok"

    monkeypatch.setattr(doubao_browser, "Future", RecordingFuture)
    gateway = DoubaoWebChatGateway(cdp_url="http://127.0.0.1:9223")
    gateway._thread = SimpleNamespace(is_alive=lambda: True)
    gateway._prompt_pacer = SimpleNamespace(seconds_until_slot=lambda: 120.0)

    try:
        assert gateway._submit("prompt", timeout_seconds=10, prompt="问题") == "ok"
    finally:
        gateway._closed = True

    assert observed_timeouts == [140.0]


@pytest.mark.parametrize(
    "marker",
    [
        "登录以解锁更多功能",
        "扫码登录",
        "下载电脑版\n登录\n有什么我能帮你的吗？",
    ],
)
def test_login_markers_pause_without_submitting(tmp_path: Path, marker: str) -> None:
    adapter = FakeAdapter([], visible_text=marker)
    gateway = DoubaoWebChatGateway(cdp_url="http://127.0.0.1:9223", adapter_factory=lambda: adapter)
    try:
        assert gateway.start().state == "waiting_for_login"
        with pytest.raises(DoubaoLoginRequired):
            gateway.discover_companies(city="深圳市", keywords=["软装"], request_text="找公司")
        assert adapter.prompts == []
        assert gateway.status().state == "waiting_for_login"
    finally:
        gateway.shutdown()


def test_challenge_marker_pauses_without_bypass(tmp_path: Path) -> None:
    adapter = FakeAdapter([], visible_text="安全验证")
    adapter.challenge_detected = lambda: True
    gateway = DoubaoWebChatGateway(cdp_url="http://127.0.0.1:9223", adapter_factory=lambda: adapter)
    try:
        gateway.start()
        with pytest.raises(DoubaoChallengeRequired):
            gateway.enrich_company(
                company_name="深圳市测试设计有限公司",
                city="深圳市",
                requested_contacts=["mobile"],
            )
        assert adapter.prompts == []
    finally:
        gateway.shutdown()


def test_visible_challenge_uses_distinct_status_from_login(tmp_path: Path) -> None:
    adapter = FakeAdapter([], visible_text="安全验证")
    adapter.challenge_detected = lambda: True
    gateway = DoubaoWebChatGateway(cdp_url="http://127.0.0.1:9223", adapter_factory=lambda: adapter)
    try:
        assert gateway.start().state == "challenge"
        assert gateway.status().state == "challenge"
    finally:
        gateway.shutdown()


def test_historical_prompt_challenge_text_does_not_pause_ready_chat() -> None:
    adapter = FakeAdapter(
        ["[]"],
        visible_text=(
            "历史对话\n"
            "遇到安全验证立即暂停并等待人工处理，不自动重试验证。\n"
            "当前对话仍可正常使用。"
        ),
    )
    gateway = DoubaoWebChatGateway(cdp_url="http://127.0.0.1:9223", adapter_factory=lambda: adapter)
    try:
        assert gateway.start().state == "ready"
        assert gateway.status().state == "ready"
    finally:
        gateway.shutdown()


def test_standalone_historical_challenge_label_does_not_pause_ready_chat() -> None:
    adapter = FakeAdapter(
        ["[]"],
        visible_text="历史对话\n安全验证\n当前对话仍可正常使用。",
    )
    gateway = DoubaoWebChatGateway(cdp_url="http://127.0.0.1:9223", adapter_factory=lambda: adapter)
    try:
        assert gateway.start().state == "ready"
    finally:
        gateway.shutdown()


def test_historical_prompt_login_text_does_not_pause_ready_chat() -> None:
    adapter = FakeAdapter(
        ["[]"],
        visible_text=(
            "历史对话\n"
            "如果页面要求扫码登录，请先确认当前账号状态。\n"
            "当前对话仍可正常使用。"
        ),
    )
    gateway = DoubaoWebChatGateway(cdp_url="http://127.0.0.1:9223", adapter_factory=lambda: adapter)
    try:
        assert gateway.start().state == "ready"
        assert gateway.status().state == "ready"
    finally:
        gateway.shutdown()


def test_cross_frame_challenge_pauses_without_bypass(tmp_path: Path) -> None:
    adapter = FakeAdapter([])
    adapter.challenge_detected = lambda: True
    gateway = DoubaoWebChatGateway(cdp_url="http://127.0.0.1:9223", adapter_factory=lambda: adapter)
    try:
        assert gateway.start().state == "challenge"
        with pytest.raises(DoubaoChallengeRequired):
            gateway.enrich_company(
                company_name="深圳市测试设计有限公司",
                city="深圳市",
                requested_contacts=["mobile"],
            )
        assert adapter.prompts == []
    finally:
        gateway.shutdown()


def test_image_challenge_dom_marker_is_detected_without_interaction() -> None:
    class CountLocator:
        def __init__(self, count: int) -> None:
            self._count = count

        def count(self) -> int:
            return self._count

        def is_visible(self) -> bool:
            return True

    class ImageChallengePage:
        url = "https://www.doubao.com/chat/123"

        def __init__(self) -> None:
            self.frames: list[object] = []

        def locator(self, selector: str) -> CountLocator:
            return CountLocator(1 if "[class*='captcha' i]" in selector else 0)

    adapter = PlaywrightDoubaoAdapter()
    adapter._page = ImageChallengePage()

    assert adapter.challenge_detected() is True


def test_hidden_captcha_dom_marker_does_not_block_ready_chat() -> None:
    class HiddenLocator:
        def count(self) -> int:
            return 1

        def is_visible(self) -> bool:
            return False

    class HiddenCaptchaPage:
        url = "https://www.doubao.com/chat/123"
        frames: list[object] = []

        def locator(self, selector: str) -> HiddenLocator:
            class EmptyLocator(HiddenLocator):
                def count(self) -> int:
                    return 0

            if selector.startswith("iframe["):
                return EmptyLocator()
            return HiddenLocator()

    adapter = PlaywrightDoubaoAdapter()
    adapter._page = HiddenCaptchaPage()

    assert adapter.challenge_detected() is False


def test_challenge_appearing_during_response_pauses_without_retry(tmp_path: Path) -> None:
    class MidResponseChallengeAdapter(FakeAdapter):
        def __init__(self) -> None:
            super().__init__([])
            self.challenge = False

        def challenge_detected(self) -> bool:
            return self.challenge

        def submit_prompt(
            self,
            prompt: str,
            *,
            timeout_seconds: int,
            cancel_event: Event | None = None,
        ) -> str:
            del cancel_event
            self.challenge = True
            raise TimeoutError("response stalled")

    adapter = MidResponseChallengeAdapter()
    gateway = DoubaoWebChatGateway(
        cdp_url="http://127.0.0.1:9223",
        adapter_factory=lambda: adapter,
    )
    try:
        assert gateway.start().state == "ready"
        with pytest.raises(DoubaoChallengeRequired):
            gateway.discover_companies(
                city="深圳市",
                keywords=["软装"],
                request_text="找公司",
            )
        assert adapter.prompts == []
    finally:
        gateway.shutdown()


def test_refresh_status_detects_login_completed(tmp_path: Path) -> None:
    adapter = FakeAdapter([], visible_text="下载电脑版\n登录\n有什么我能帮你的吗？")
    gateway = DoubaoWebChatGateway(cdp_url="http://127.0.0.1:9223", adapter_factory=lambda: adapter)
    try:
        assert gateway.start().state == "waiting_for_login"
        with pytest.raises(DoubaoLoginRequired):
            gateway.discover_companies(city="深圳市", keywords=["软装"], request_text="找公司")

        adapter.page_text = "豆包对话页面"

        assert gateway.refresh_status().state == "ready"
    finally:
        gateway.shutdown()


def test_shutdown_rejects_new_commands(tmp_path: Path) -> None:
    adapter = FakeAdapter([])
    gateway = DoubaoWebChatGateway(cdp_url="http://127.0.0.1:9223", adapter_factory=lambda: adapter)
    gateway.start()
    gateway.shutdown()

    assert adapter.closed is True
    with pytest.raises(DoubaoWebUnavailable):
        gateway.status()


def test_prompt_rate_limit_waits_between_requests_and_cools_down_after_batch() -> None:
    adapter = FakeAdapter(
        [
            "1｜深圳市测试设计有限公司｜https://example.cn/about｜主营别墅全案",
            "1｜深圳市测试设计有限公司｜https://example.cn/about｜主营别墅全案",
            "1｜深圳市测试设计有限公司｜https://example.cn/about｜主营别墅全案",
        ]
    )
    delays: list[float] = []
    gateway = DoubaoWebChatGateway(
        cdp_url="http://127.0.0.1:9223",
        adapter_factory=lambda: adapter,
        request_interval_min_seconds=1,
        request_interval_max_seconds=1,
        batch_size=2,
        cooldown_min_seconds=5,
        cooldown_max_seconds=5,
        sleep_fn=delays.append,
    )
    try:
        gateway.start()
        for _ in range(3):
            gateway.discover_companies(
                city="深圳市",
                keywords=["别墅软装"],
                request_text="查找别墅软装公司",
            )
    finally:
        gateway.shutdown()

    assert delays == pytest.approx([1, 5], abs=0.01)


def test_status_reports_working_while_waiting_for_prompt_rate_limit() -> None:
    adapter = FakeAdapter(
        [
            "1｜深圳市测试设计有限公司｜https://example.cn/about｜主营别墅全案",
            "1｜深圳市测试设计有限公司｜https://example.cn/about｜主营别墅全案",
        ]
    )
    sleep_started = Event()
    release_sleep = Event()

    def blocked_sleep(_seconds: float) -> None:
        sleep_started.set()
        release_sleep.wait(timeout=2)

    gateway = DoubaoWebChatGateway(
        cdp_url="http://127.0.0.1:9223",
        adapter_factory=lambda: adapter,
        request_interval_min_seconds=1,
        request_interval_max_seconds=1,
        batch_size=10,
        sleep_fn=blocked_sleep,
    )
    try:
        gateway.start()
        gateway.discover_companies(
            city="深圳市",
            keywords=["别墅软装"],
            request_text="查找别墅软装公司",
        )
        worker = Thread(
            target=lambda: gateway.discover_companies(
                city="深圳市",
                keywords=["别墅软装"],
                request_text="查找别墅软装公司",
            ),
            daemon=True,
        )
        worker.start()
        assert sleep_started.wait(timeout=1)
        status = gateway.status()
        assert status.state == "working"
        assert "剩余" in status.detail
        release_sleep.set()
        worker.join(timeout=2)
    finally:
        release_sleep.set()
        gateway.shutdown()


def test_prompt_pacing_wait_is_not_reported_as_timeout_before_slot() -> None:
    adapter = FakeAdapter(
        [
            "1｜深圳市测试设计有限公司｜https://example.cn/about｜主营别墅全案",
            "1｜深圳市第二家测试设计有限公司｜https://example.cn/about｜主营别墅全案",
        ]
    )
    sleep_started = Event()
    release_sleep = Event()

    def blocking_sleep(seconds: float) -> None:
        if seconds > 1:
            sleep_started.set()
            release_sleep.wait(timeout=30)

    gateway = DoubaoWebChatGateway(
        cdp_url="http://127.0.0.1:9223",
        request_timeout_seconds=1,
        request_interval_min_seconds=60,
        request_interval_max_seconds=60,
        batch_size=10,
        sleep_fn=blocking_sleep,
        adapter_factory=lambda: adapter,
    )
    timeout_done = Event()
    timeout_result: dict[str, BaseException] = {}

    try:
        gateway.start()
        gateway.discover_companies(
            city="深圳市",
            keywords=["别墅软装"],
            request_text="查找深圳别墅软装公司",
        )

        def wait_for_second_prompt() -> None:
            try:
                gateway.discover_companies(
                    city="深圳市",
                    keywords=["别墅软装"],
                    request_text="查找深圳别墅软装公司",
                )
            except Exception as exc:  # pragma: no cover - asserted below
                timeout_result["error"] = exc
            finally:
                timeout_done.set()

        worker = Thread(target=wait_for_second_prompt, daemon=True)
        worker.start()
        assert sleep_started.wait(timeout=2)
        assert not timeout_done.wait(timeout=2)

        release_sleep.set()
        assert timeout_done.wait(timeout=2)
        assert "error" not in timeout_result

        reconnect_done = Event()
        reconnect_result: dict[str, object] = {}

        def reconnect() -> None:
            try:
                reconnect_result["status"] = gateway.reconnect()
            except Exception as exc:  # pragma: no cover - asserted below
                reconnect_result["error"] = exc
            finally:
                reconnect_done.set()

        reconnect_worker = Thread(target=reconnect, daemon=True)
        reconnect_worker.start()
        assert reconnect_done.wait(timeout=2)
        assert "error" not in reconnect_result
    finally:
        release_sleep.set()
        gateway.shutdown()


def test_status_returns_immediately_while_browser_is_connecting() -> None:
    started = Event()
    release = Event()
    status_done = Event()
    status_result: dict[str, object] = {}

    class BlockingAdapter(FakeAdapter):
        def start(self, *, cdp_url: str, chat_url: str) -> None:
            started.set()
            release.wait(timeout=2)
            super().start(cdp_url=cdp_url, chat_url=chat_url)

    gateway = DoubaoWebChatGateway(
        cdp_url="http://127.0.0.1:9223",
        request_timeout_seconds=1,
        adapter_factory=lambda: BlockingAdapter([]),
    )
    starter = Thread(target=gateway.start, daemon=True)
    starter.start()
    assert started.wait(timeout=1)

    def read_status() -> None:
        try:
            status_result["value"] = gateway.status()
        except (DoubaoWebUnavailable, TimeoutError) as exc:  # pragma: no cover - failure detail for the assertion below
            status_result["error"] = exc
        finally:
            status_done.set()

    reader = Thread(target=read_status, daemon=True)
    begin = monotonic()
    reader.start()

    assert status_done.wait(timeout=0.5)
    assert monotonic() - begin < 0.5
    assert getattr(status_result.get("value"), "state", None) == "connecting"

    release.set()
    starter.join(timeout=2)
    gateway.shutdown()


def test_status_returns_immediately_while_browser_command_is_running() -> None:
    started = Event()
    release = Event()
    status_done = Event()
    status_result: dict[str, object] = {}

    class BlockingPromptAdapter(FakeAdapter):
        def submit_prompt(
            self,
            prompt: str,
            *,
            timeout_seconds: int,
            cancel_event: Event | None = None,
        ) -> str:
            del cancel_event
            started.set()
            release.wait(timeout=2)
            return super().submit_prompt(prompt, timeout_seconds=timeout_seconds)

    adapter = BlockingPromptAdapter(
        ["1|深圳市测试设计有限公司|https://example.cn/about|别墅软装"]
    )
    gateway = DoubaoWebChatGateway(
        cdp_url="http://127.0.0.1:9223",
        request_timeout_seconds=1,
        adapter_factory=lambda: adapter,
    )
    gateway.start()
    worker = Thread(
        target=lambda: gateway.discover_companies(
            city="深圳市",
            keywords=["别墅软装"],
            request_text="查找别墅软装公司",
        ),
        daemon=True,
    )
    worker.start()
    assert started.wait(timeout=1)

    def read_status() -> None:
        try:
            status_result["value"] = gateway.status()
        except (DoubaoWebUnavailable, TimeoutError) as exc:  # pragma: no cover - failure detail for the assertion below
            status_result["error"] = exc
        finally:
            status_done.set()

    begin = monotonic()
    reader = Thread(target=read_status, daemon=True)
    reader.start()

    assert status_done.wait(timeout=0.5)
    assert monotonic() - begin < 0.5
    assert getattr(status_result.get("value"), "state", None) == "working"

    release.set()
    worker.join(timeout=2)
    gateway.shutdown()


def test_status_does_not_queue_behind_slow_status_refresh() -> None:
    refresh_started = Event()
    release_refresh = Event()
    status_done = Event()
    status_result: dict[str, object] = {}

    class BlockingVisibleTextAdapter(FakeAdapter):
        block_visible_text = False

        def visible_text(self) -> str:
            if self.block_visible_text:
                refresh_started.set()
                release_refresh.wait(timeout=2)
            return super().visible_text()

    adapter = BlockingVisibleTextAdapter([])
    gateway = DoubaoWebChatGateway(
        cdp_url="http://127.0.0.1:9223",
        request_timeout_seconds=1,
        adapter_factory=lambda: adapter,
    )
    gateway.start()
    adapter.block_visible_text = True
    refresh_worker = Thread(target=gateway.refresh_status, daemon=True)
    refresh_worker.start()
    assert refresh_started.wait(timeout=1)

    def read_status() -> None:
        try:
            status_result["value"] = gateway.status()
        except Exception as exc:  # pragma: no cover - failure detail for assertion
            status_result["error"] = exc
        finally:
            status_done.set()

    status_worker = Thread(target=read_status, daemon=True)
    status_worker.start()
    assert status_done.wait(timeout=0.5)
    assert getattr(status_result.get("value"), "state", None) == "ready"

    release_refresh.set()
    refresh_worker.join(timeout=2)
    status_worker.join(timeout=2)
    gateway.shutdown()


def test_adapter_skips_when_no_response_starts_and_page_is_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Editor:
        def fill(self, _prompt: str) -> None:
            pass

        def press(self, _key: str) -> None:
            pass

    class IdleAdapter(PlaywrightDoubaoAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.latest_calls = 0

        def _find_editor(self) -> Editor:
            return Editor()

        def _latest_response_text(self) -> str:
            self.latest_calls += 1
            return "old response" if self.latest_calls == 1 else "query"

        def _is_generating(self) -> bool:
            return False

    clock = [-1.0]

    def fake_monotonic() -> float:
        clock[0] += 1
        return clock[0]

    monkeypatch.setattr("app.collection.doubao_browser.monotonic", fake_monotonic)
    monkeypatch.setattr("app.collection.doubao_browser.sleep", lambda _seconds: None)

    with pytest.raises(DoubaoWebTimeout):
        IdleAdapter().submit_prompt("query", timeout_seconds=180)

    assert clock[0] < 100


def test_adapter_allows_slow_search_response_to_start_before_timing_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Editor:
        def fill(self, _prompt: str) -> None:
            pass

        def press(self, _key: str) -> None:
            pass

    class SlowStartAdapter(PlaywrightDoubaoAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.latest_calls = 0

        def _find_editor(self) -> Editor:
            return Editor()

        def _latest_response_text(self) -> str:
            self.latest_calls += 1
            return "old response" if self.latest_calls <= 45 else "new answer"

        def _is_generating(self) -> bool:
            return False

    clock = [-1.0]

    def fake_monotonic() -> float:
        clock[0] += 1
        return clock[0]

    monkeypatch.setattr("app.collection.doubao_browser.monotonic", fake_monotonic)
    monkeypatch.setattr("app.collection.doubao_browser.sleep", lambda _seconds: None)

    assert SlowStartAdapter().submit_prompt("query", timeout_seconds=180) == "new answer"


def test_installed_browser_channel_falls_back_to_edge(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.collection.doubao_browser.Path.exists",
        lambda path: "Microsoft\\Edge" in str(path),
    )

    assert installed_browser_channel() == "msedge"
