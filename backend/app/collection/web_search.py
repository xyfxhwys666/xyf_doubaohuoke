import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from app.collection.cities import tier3_plus_city_names
from app.collection.providers import ProviderError

MOBILE_PATTERN = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
EMAIL_PATTERN = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
WECHAT_PATTERN = re.compile(
    r"(?:微信(?:号|ID)?|WeChat)\s*[:：]?\s*([A-Za-z][A-Za-z0-9_-]{5,19})",
    re.IGNORECASE,
)
_NON_OFFICIAL_HOSTS = {
    "qcc.com",
    "tianyancha.com",
    "aiqicha.baidu.com",
    "bosszhipin.com",
    "58.com",
    "sogou.com",
    "so.com",
    "baidu.com",
}
_URL_HYPHEN_TRANSLATION = str.maketrans(
    {"\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2212": "-"}
)


def _normalize_public_url(value: str | None) -> str:
    return str(value or "").strip().translate(_URL_HYPHEN_TRANSLATION)


def _is_official_candidate(url: str) -> bool:
    host = urlparse(url).netloc.casefold().split(":", 1)[0]
    host = host.removeprefix("www.")
    return bool(host) and not any(
        host == blocked or host.endswith(f".{blocked}") for blocked in _NON_OFFICIAL_HOSTS
    )


@dataclass(frozen=True, slots=True)
class CompanyNameEvidence:
    status: str
    evidence_urls: list[str]
    reason: str
    city_matched: bool
    keyword_matched: bool


@dataclass(frozen=True, slots=True)
class ContactEvidence:
    status: str
    evidence_url: str | None
    evidence_quote: str
    reason: str
    contact_person_name: str | None = None
    contact_person_role: str | None = None
    evidence_urls: list[str] | None = None


class _VerificationDeadlineExceeded(RuntimeError):
    """Stop one slow contact lookup without aborting the collection task."""


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._ignored_tag: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if self._ignored_tag is None and tag in {"script", "style", "noscript", "svg", "template"}:
            self._ignored_tag = tag

    def handle_endtag(self, tag: str) -> None:
        if tag == self._ignored_tag:
            self._ignored_tag = None

    def handle_data(self, data: str) -> None:
        if self._ignored_tag is not None:
            return
        value = data.strip()
        if value:
            self.parts.append(value)


class _SearchResultParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict[str, str]] = []
        self._in_result = False
        self._result_tag = ""
        self._result_depth = 0
        self._in_heading = False
        self._href = ""
        self._title: list[str] = []
        self._snippet: list[str] = []
        self._ignored_tag: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._ignored_tag is not None:
            return
        if tag in {"script", "style", "noscript", "svg", "template"}:
            self._ignored_tag = tag
            return
        values = dict(attrs)
        classes = (values.get("class") or "").split()
        if not self._in_result and tag in {"li", "div"} and ({"b_algo", "vrwrap", "res-list"} & set(classes)):
            self._in_result = True
            self._result_tag = tag
            self._result_depth = 1
            self._href = ""
            self._title = []
            self._snippet = []
        elif self._in_result:
            if tag == self._result_tag:
                self._result_depth += 1
            if tag in {"h2", "h3"}:
                self._in_heading = True
            elif self._in_heading and tag == "a" and not self._href:
                self._href = values.get("href") or ""

    def handle_endtag(self, tag: str) -> None:
        if tag == self._ignored_tag:
            self._ignored_tag = None
            return
        if self._ignored_tag is not None:
            return
        if not self._in_result:
            return
        if tag in {"h2", "h3"}:
            self._in_heading = False
        if tag != self._result_tag:
            return
        self._result_depth -= 1
        if self._result_depth == 0:
            if self._href and self._title:
                self.results.append(
                    {
                        "url": self._href,
                        "title": " ".join(self._title).strip(),
                        "snippet": " ".join(self._snippet).strip(),
                    }
                )
            self._in_result = False

    def handle_data(self, data: str) -> None:
        if self._ignored_tag is not None:
            return
        value = data.strip()
        if not self._in_result or not value:
            return
        (self._title if self._in_heading else self._snippet).append(value)


class _ContactLinkParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__()
        self.base_url = base_url
        self.base_host = urlparse(base_url).netloc
        self.links: list[str] = []
        self._href = ""
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self._href = dict(attrs).get("href") or ""
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href and data.strip():
            self._text.append(data.strip())

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or not self._href:
            return
        target = urljoin(self.base_url, self._href)
        parsed = urlparse(target)
        label = "".join(self._text).lower()
        path = parsed.path.lower()
        markers = ("联系我们", "联系方式", "业务咨询", "contact", "contact-us", "about")
        if (
            parsed.scheme in {"http", "https"}
            and parsed.netloc == self.base_host
            and any(marker in label or marker in path for marker in markers)
            and target not in self.links
        ):
            self.links.append(target)
        self._href = ""
        self._text = []


def _visible_text(html: str) -> str:
    parser = _TextParser()
    parser.feed(html)
    return " ".join(parser.parts)


COMPANY_PATTERN = re.compile(
    r"[\u4e00-\u9fffA-Za-z0-9（）()·]{2,48}?(?:有限责任公司|有限公司|设计公司|装饰公司|公司|工作室|设计院|事务所|中心)"
)


def clean_company_name(title: str) -> str:
    cleaned = re.sub(r"^\s*(?:联系方式|联系我们|联系电话)\s*[-_|—–·]?\s*", "", title)
    cleaned = re.sub(r"(?:电话号码|联系电话|联系方式|联系我们|官网)\s*$", "", cleaned)
    cleaned = re.sub(r"\s+", "", cleaned)
    formal_name = "有限公司" in cleaned or "有限责任公司" in cleaned
    generic_terms = (
        "本地装修公司",
        "专业软装公司",
        "最好的软装公司",
        "公司推荐",
        "推荐排行",
        "排行榜",
        "哪家好",
        "十大公司",
    )
    portal_prefixes = ("搜了网", "58同城", "BOSS直聘", "百度百科")
    if not formal_name and (
        any(term in cleaned for term in generic_terms)
        or any(cleaned.startswith(prefix) for prefix in portal_prefixes)
    ):
        return ""
    matches = COMPANY_PATTERN.findall(cleaned)
    return matches[-1] if matches else ""


def clean_service_intro(
    text: str,
    *,
    contacts: list[str | None],
    company_name: str,
) -> str:
    cleaned = text
    cleaned = re.sub(r"https?://\S+|www\.\S+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(
        r"\b(?:[A-Za-z0-9-]+\.)+(?:com|cn|net|org)\b",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\b(?:window|document)\.[^；;。]*[；;]?", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\.[A-Za-z_-][A-Za-z0-9_-]*\s*\{[^{}]*\}", "", cleaned)
    cleaned = re.sub(r"(?:爱企查|水滴信用|天眼查|企查查)?反馈", "", cleaned)
    cleaned = re.sub(r"取消立刻核查|我要催收债款|点击复制|更多\d*|官网", "", cleaned)
    cleaned = re.sub(r"(?:公司)?简介\s*[:：]?", "", cleaned)
    for contact in contacts:
        if contact:
            cleaned = cleaned.replace(contact, "")
    cleaned = re.sub(r"联系人\s*[:：]\s*[^，,。；;\s]{1,12}\s*[，,。；;]?", "", cleaned)
    cleaned = re.sub(r"(?:电话|手机|邮箱|邮件|微信(?:号|ID)?)\s*[:：]?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = EMAIL_PATTERN.sub("", cleaned)
    cleaned = MOBILE_PATTERN.sub("", cleaned)
    cleaned = re.sub(r"(?<!\d)0\d{2,3}[-\s]?\d{7,8}(?!\d)", "", cleaned)
    cleaned = re.sub(r"[A-Za-z][A-Za-z0-9_-]*", "", cleaned)
    if company_name:
        cleaned = cleaned.replace(company_name, "")
    cleaned = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9，。；：、（）()\-+& ]+", "", cleaned)
    cleaned = re.sub(r"\s+", "", cleaned)
    cleaned = re.sub(r"^[，,。；;：:\-_]+|[，,。；;：:\-_]+$", "", cleaned)
    return cleaned[:180]


def select_service_intro(
    snippet: str,
    page_text: str,
    *,
    contacts: list[str | None],
    company_name: str,
    keyword: str,
) -> str:
    segments = [snippet, *re.split(r"[。！？\r\n]", page_text)]
    business_terms = ("软装", "设计", "装饰", "整装", "家居", "家具", "空间", "服务")
    candidates: list[tuple[int, str]] = []
    for segment in segments:
        cleaned = clean_service_intro(
            segment,
            contacts=contacts,
            company_name=company_name,
        )
        chinese_count = len(re.findall(r"[\u4e00-\u9fff]", cleaned))
        if chinese_count < 8 or not any(term in cleaned for term in business_terms):
            continue
        keyword_core = re.sub(r"公司$", "", keyword)
        score = min(chinese_count, 180) + (50 if keyword_core and keyword_core in cleaned else 0)
        candidates.append((score, cleaned))
    if candidates:
        return max(candidates, key=lambda item: item[0])[1]
    return keyword


class WebSearchProvider:
    id = "web_search"
    label = "公开网页搜索"
    configured = True

    def __init__(
        self,
        *,
        search_url: str = "https://www.sogou.com/web",
        max_results: int = 5,
        max_search_pages: int = 1,
        request_interval_seconds: float = 0.4,
        retry_limit: int = 2,
        detail_workers: int = 6,
        contact_verification_timeout_seconds: float = 45.0,
        max_contact_source_urls: int = 4,
        client: httpx.Client | None = None,
    ) -> None:
        self.search_url = search_url
        self.max_results = max_results
        self.max_search_pages = max_search_pages
        self.request_interval_seconds = request_interval_seconds
        self.retry_limit = retry_limit
        self.detail_workers = detail_workers
        self.contact_verification_timeout_seconds = max(
            0.0,
            float(contact_verification_timeout_seconds),
        )
        self.max_contact_source_urls = max(1, int(max_contact_source_urls))
        self.client = client or httpx.Client(timeout=8, follow_redirects=True)

    def _get(
        self,
        url: str,
        *,
        deadline: float | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        for attempt in range(self.retry_limit + 1):
            request_kwargs = dict(kwargs)
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _VerificationDeadlineExceeded
                configured_timeout = request_kwargs.get("timeout")
                request_kwargs["timeout"] = (
                    min(float(configured_timeout), remaining)
                    if configured_timeout is not None
                    else remaining
                )
            try:
                response = self.client.get(url, **request_kwargs)
                response.raise_for_status()
                return response
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 429 and exc.response.status_code < 500:
                    raise
                if attempt >= self.retry_limit:
                    raise
                delay = max(self.request_interval_seconds, 0.5 * (attempt + 1))
            except httpx.RequestError:
                if attempt >= self.retry_limit:
                    raise
                delay = max(self.request_interval_seconds, 0.5 * (attempt + 1))
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _VerificationDeadlineExceeded
                delay = min(delay, remaining)
            time.sleep(delay)
        raise ProviderError("public web search retry limit exhausted")

    def list_cities(self) -> list[str]:
        raise ProviderError("web search does not provide a city catalog")

    def verify_company_name(
        self,
        *,
        company_name: str,
        city: str,
        keywords: list[str],
        source_urls: list[str] | None = None,
    ) -> CompanyNameEvidence:
        normalized_name = re.sub(r"\s+", "", company_name).casefold()
        city_short = city.removesuffix("市")
        keyword_terms = [
            re.sub(r"公司$", "", keyword.strip())
            for keyword in keywords
            if len(re.sub(r"公司$", "", keyword.strip())) >= 2
        ]
        query = " ".join([city, company_name, *keyword_terms[:3]])
        supplied_sources = list(
            dict.fromkeys(_normalize_public_url(value) for value in source_urls or [])
        )
        result_rows: list[tuple[str, dict[str, str]]] = [
            (source_url, {"url": source_url})
            for source_url in supplied_sources
            if urlparse(source_url).scheme in {"http", "https"}
            and urlparse(source_url).netloc
        ]
        indexed_evidence_by_url: dict[str, dict[str, Any]] = {}
        successful_searches = 0
        known_cities = tier3_plus_city_names()

        for page_index in range(self.max_search_pages):
            sources = (
                (self.search_url, {"query": query, "page": page_index + 1}),
                ("https://www.so.com/s", {"q": query, "pn": page_index + 1}),
                (
                    "https://cn.bing.com/search",
                    {"q": query, "first": page_index * 10 + 1},
                ),
            )
            for source_url, params in sources:
                if self.request_interval_seconds:
                    time.sleep(self.request_interval_seconds)
                try:
                    response = self._get(
                        source_url,
                        params=params,
                        headers={"User-Agent": "Mozilla/5.0 (compatible; CityLeadPlatform/1.0)"},
                    )
                except httpx.HTTPError:
                    continue
                successful_searches += 1
                parser = _SearchResultParser()
                parser.feed(response.text)
                for result in parser.results[: self.max_results]:
                    result_rows.append((str(response.url), result))
                    result_url = _normalize_public_url(
                        urljoin(str(response.url), result["url"])
                    )
                    parsed_result = urlparse(result_url)
                    if (
                        parsed_result.scheme not in {"http", "https"}
                        or not parsed_result.netloc
                    ):
                        continue
                    indexed_text = f"{result['title']} {result['snippet']}"
                    compact_indexed = re.sub(r"\s+", "", indexed_text).casefold()
                    if normalized_name not in compact_indexed:
                        continue
                    signal_text = compact_indexed.replace(normalized_name, "")
                    mentioned_cities = {
                        name for name in known_cities if name.casefold() in signal_text
                    }
                    conflicting_city = any(name != city for name in mentioned_cities)
                    indexed_evidence_by_url.setdefault(
                        result_url,
                        {
                            "url": result_url,
                            "host": parsed_result.netloc.casefold(),
                            "city_matched": not conflicting_city
                            and (
                                city.casefold() in signal_text
                                or city_short.casefold() in signal_text
                            ),
                            "keyword_matched": any(
                                term.casefold() in signal_text for term in keyword_terms
                            ),
                            "conflicting_city": conflicting_city,
                        },
                    )

        if successful_searches == 0 and not result_rows:
            return CompanyNameEvidence(
                status="verification_failed",
                evidence_urls=[],
                reason="公开网页搜索请求全部失败",
                city_matched=False,
                keyword_matched=False,
            )

        evidence_by_url: dict[str, dict[str, Any]] = {}
        for search_page_url, result in result_rows:
            result_url = _normalize_public_url(urljoin(search_page_url, result["url"]))
            parsed_result = urlparse(result_url)
            if parsed_result.scheme not in {"http", "https"} or not parsed_result.netloc:
                continue
            if self.request_interval_seconds:
                time.sleep(self.request_interval_seconds)
            try:
                page = self._get(
                    result_url,
                    headers={"User-Agent": "Mozilla/5.0 (compatible; CityLeadPlatform/1.0)"},
                )
            except httpx.HTTPError:
                continue
            source_url = str(page.url)
            parsed_source = urlparse(source_url)
            if parsed_source.scheme not in {"http", "https"} or not parsed_source.netloc:
                continue
            compact_text = re.sub(r"\s+", "", _visible_text(page.text)).casefold()
            if normalized_name not in compact_text:
                continue
            signal_text = compact_text.replace(normalized_name, "")
            mentioned_cities = {name for name in known_cities if name in signal_text}
            conflicting_city = any(name != city for name in mentioned_cities)
            city_matched = not conflicting_city and (
                city.casefold() in signal_text or city_short.casefold() in signal_text
            )
            keyword_matched = any(term.casefold() in signal_text for term in keyword_terms)
            evidence_by_url.setdefault(
                source_url,
                {
                    "url": source_url,
                    "host": parsed_source.netloc.casefold(),
                    "city_matched": city_matched,
                    "keyword_matched": keyword_matched,
                    "conflicting_city": conflicting_city,
                },
            )

        evidence = list(evidence_by_url.values())
        for item in evidence:
            if (
                item["city_matched"]
                and item["keyword_matched"]
                and _is_official_candidate(item["url"])
            ):
                return CompanyNameEvidence(
                    status="verified_official_page",
                    evidence_urls=[item["url"]],
                    reason="单一公开网页同时确认完整公司名称、城市和主营方向",
                    city_matched=True,
                    keyword_matched=True,
                )

        for index, first in enumerate(evidence):
            for second in evidence[index + 1 :]:
                if first["host"] == second["host"]:
                    continue
                if first["conflicting_city"] or second["conflicting_city"]:
                    continue
                city_matched = first["city_matched"] or second["city_matched"]
                keyword_matched = first["keyword_matched"] or second["keyword_matched"]
                if city_matched and keyword_matched:
                    return CompanyNameEvidence(
                        status="verified_cross_source",
                        evidence_urls=[first["url"], second["url"]],
                        reason="两个不同域名的公开网页共同确认完整公司名称、城市和主营方向",
                        city_matched=True,
                        keyword_matched=True,
                    )

        indexed_evidence = list(indexed_evidence_by_url.values())
        if not any(item["conflicting_city"] for item in evidence):
            for item in indexed_evidence:
                if (
                    item["city_matched"]
                    and item["keyword_matched"]
                    and not item["conflicting_city"]
                ):
                    return CompanyNameEvidence(
                        status="verified_public_web",
                        evidence_urls=[item["url"]],
                        reason="公开搜索结果同时确认完整公司名称、城市和主营方向",
                        city_matched=True,
                        keyword_matched=True,
                    )

        if any(item["conflicting_city"] for item in evidence):
            return CompanyNameEvidence(
                status="rejected",
                evidence_urls=[item["url"] for item in evidence],
                reason="公开网页中的公司所在城市与目标城市冲突",
                city_matched=False,
                keyword_matched=any(item["keyword_matched"] for item in evidence),
            )
        return CompanyNameEvidence(
            status="unverified",
            evidence_urls=[item["url"] for item in evidence],
            reason="公开网页证据不足，无法同时确认完整公司名称、城市和主营方向",
            city_matched=any(item["city_matched"] for item in evidence),
            keyword_matched=any(item["keyword_matched"] for item in evidence),
        )

    def verify_contact_evidence(
        self,
        *,
        company_name: str,
        mobile: str | None,
        email: str | None = None,
        source_url: str | None,
        source_urls: list[str] | None = None,
        contact_person_name: str | None,
        contact_person_role: str | None,
    ) -> ContactEvidence:
        normalized_name = re.sub(r"\s+", "", company_name).casefold()
        normalized_mobile = re.sub(r"\D", "", str(mobile or ""))
        normalized_email = str(email or "").strip().casefold()
        if not EMAIL_PATTERN.fullmatch(normalized_email):
            normalized_email = ""
        candidate_urls = list(
            dict.fromkeys(
                _normalize_public_url(value)
                for value in [str(source_url or "").strip(), *(source_urls or [])]
                if value
            )
        )

        if not normalized_name:
            return ContactEvidence(
                status="rejected_mismatch",
                evidence_url=None,
                evidence_quote="",
                reason="公司全称为空",
            )
        if not re.fullmatch(r"1[3-9]\d{9}", normalized_mobile):
            normalized_mobile = ""
        if not normalized_mobile and not normalized_email:
            return ContactEvidence(
                status="rejected_mismatch",
                evidence_url=candidate_urls[0] if candidate_urls else None,
                evidence_quote="",
                reason="未提供有效的中国大陆 11 位手机号或公开邮箱",
            )
        valid_urls = [
            value
            for value in candidate_urls
            if urlparse(value).scheme in {"http", "https"} and urlparse(value).netloc
        ]
        if not valid_urls:
            return ContactEvidence(
                status="unverified_model_only",
                evidence_url=None,
                evidence_quote="",
                reason="缺少可重新访问的 HTTP(S) 来源链接",
                evidence_urls=[],
            )

        deadline = time.monotonic() + self.contact_verification_timeout_seconds
        valid_urls = valid_urls[: self.max_contact_source_urls]

        fetched: list[dict[str, Any]] = []
        failed_urls: list[str] = []
        login_urls: list[str] = []
        timed_out = False
        for candidate_url in valid_urls:
            try:
                page = self._get(
                    candidate_url,
                    deadline=deadline,
                    headers={"User-Agent": "Mozilla/5.0 (compatible; CityLeadPlatform/1.0)"},
                )
            except _VerificationDeadlineExceeded:
                timed_out = True
                break
            except httpx.HTTPError:
                failed_urls.append(candidate_url)
                continue
            evidence_url = str(page.url)
            page_text = _visible_text(page.text)
            compact_text = re.sub(r"\s+", "", page_text).casefold()
            if any(marker in compact_text for marker in ("请登录", "验证码登录", "登录后继续")):
                login_urls.append(evidence_url)
                continue
            fetched.append(
                {
                    "url": evidence_url,
                    "domain": urlparse(evidence_url).netloc.casefold(),
                    "text": page_text,
                    "name_matched": normalized_name in compact_text,
                    "mobiles": set(MOBILE_PATTERN.findall(page_text)),
                    "emails": {
                        value.casefold() for value in EMAIL_PATTERN.findall(page_text)
                    },
                }
            )

        def contact_matches(item: dict[str, Any]) -> bool:
            return bool(
                (normalized_mobile and normalized_mobile in item["mobiles"])
                or (normalized_email and normalized_email in item["emails"])
            )

        matching = [
            item
            for item in fetched
            if item["name_matched"] and contact_matches(item)
        ]
        if not matching:
            contact_token = normalized_mobile or normalized_email
            query = f'"{company_name}" "{contact_token}"'
            search_sources = (
                (self.search_url, {"query": query}),
                ("https://www.so.com/s", {"q": query}),
                ("https://cn.bing.com/search", {"q": query}),
            )
            for search_url, params in search_sources:
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
                if self.request_interval_seconds:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        timed_out = True
                        break
                    time.sleep(min(self.request_interval_seconds, remaining))
                try:
                    response = self._get(
                        search_url,
                        deadline=deadline,
                        params=params,
                        headers={
                            "User-Agent": "Mozilla/5.0 (compatible; CityLeadPlatform/1.0)"
                        },
                    )
                except _VerificationDeadlineExceeded:
                    timed_out = True
                    break
                except httpx.HTTPError:
                    continue
                parser = _SearchResultParser()
                parser.feed(response.text)
                for result in parser.results[: self.max_results]:
                    indexed_text = f"{result['title']} {result['snippet']}"
                    compact_indexed = re.sub(r"\s+", "", indexed_text).casefold()
                    if (
                        normalized_name not in compact_indexed
                        or contact_token not in compact_indexed
                    ):
                        continue
                    result_url = _normalize_public_url(
                        urljoin(str(response.url), result["url"])
                    )
                    parsed_result = urlparse(result_url)
                    if parsed_result.scheme not in {"http", "https"} or not parsed_result.netloc:
                        continue
                    fetched.append(
                        {
                            "url": result_url,
                            "domain": parsed_result.netloc.casefold(),
                            "text": indexed_text,
                            "name_matched": True,
                            "mobiles": {normalized_mobile} if normalized_mobile else set(),
                            "emails": {
                                value.casefold()
                                for value in EMAIL_PATTERN.findall(indexed_text)
                            },
                        }
                    )
            matching = [
                item
                for item in fetched
                if item["name_matched"] and contact_matches(item)
            ]
        if timed_out and not matching:
            return ContactEvidence(
                status="verification_timeout",
                evidence_url=fetched[0]["url"] if fetched else valid_urls[0],
                evidence_quote="",
                reason="联系方式公开证据核验达到单家公司时限，已保留原始联系方式",
                evidence_urls=[item["url"] for item in fetched],
            )
        conflicting = [
            item
            for item in fetched
            if normalized_mobile
            and item["name_matched"]
            and item["mobiles"]
            and normalized_mobile not in item["mobiles"]
        ]
        if conflicting:
            return ContactEvidence(
                status="rejected_mismatch",
                evidence_url=conflicting[0]["url"],
                evidence_quote="",
                reason="不同公开页面中的手机号存在冲突",
                evidence_urls=[item["url"] for item in conflicting],
            )

        distinct_matching: dict[str, dict[str, Any]] = {}
        for item in matching:
            distinct_matching.setdefault(item["domain"], item)
        if len(distinct_matching) >= 2:
            selected = list(distinct_matching.values())[:2]
            page_text = selected[0]["text"]
            mobile_index = page_text.find(normalized_mobile)
            quote_start = max(0, mobile_index - 100)
            quote_end = min(len(page_text), mobile_index + len(normalized_mobile) + 100)
            return ContactEvidence(
                status="verified_cross_source",
                evidence_url=selected[0]["url"],
                evidence_quote=page_text[quote_start:quote_end].strip(),
                reason="两个不同域名的公开页面同时确认完整公司名称和相同手机号",
                evidence_urls=[item["url"] for item in selected],
            )

        if not matching:
            if login_urls:
                return ContactEvidence(
                    status="pending_login_source",
                    evidence_url=login_urls[0],
                    evidence_quote="",
                    reason="来源页面需要登录后才能核验",
                    evidence_urls=login_urls,
                )
            if failed_urls and not fetched:
                return ContactEvidence(
                    status="verification_failed",
                    evidence_url=failed_urls[0],
                    evidence_quote="",
                    reason="联系方式来源页面请求失败",
                )
            reason = (
                "来源页面正文未出现完整公司全称"
                if not any(item["name_matched"] for item in fetched)
                else "来源页面正文未出现相同手机号"
            )
            return ContactEvidence(
                status="rejected_mismatch",
                evidence_url=fetched[0]["url"] if fetched else valid_urls[0],
                evidence_quote="",
                reason=reason,
                evidence_urls=[item["url"] for item in fetched],
            )

        evidence_url = matching[0]["url"]
        page_text = matching[0]["text"]
        contact_value = normalized_mobile or normalized_email
        contact_index = page_text.casefold().find(contact_value)
        quote_start = max(0, contact_index - 100)
        quote_end = min(len(page_text), contact_index + len(contact_value) + 100)
        evidence_quote = page_text[quote_start:quote_end].strip()
        person_name = str(contact_person_name or "").strip() or None
        person_role = str(contact_person_role or "").strip() or None
        if person_name and person_name not in evidence_quote:
            person_name = None
        if person_role and person_role not in evidence_quote:
            person_role = None
        is_official = _is_official_candidate(evidence_url)
        return ContactEvidence(
            status=("verified_official_page" if is_official else "verified_public_web"),
            evidence_url=evidence_url,
            evidence_quote=evidence_quote,
            reason=(
                "来源页面正文同时确认完整公司名称和手机号或邮箱"
                if is_official
                else "公开第三方页面同时确认完整公司名称和手机号或邮箱"
            ),
            contact_person_name=person_name,
            contact_person_role=person_role,
            evidence_urls=[evidence_url],
        )

    def _lead_from_result(
        self,
        search_page_url: str,
        result: dict[str, str],
        keyword: str,
    ) -> dict[str, Any] | None:
        result_url = urljoin(search_page_url, result["url"])
        if self.request_interval_seconds:
            time.sleep(self.request_interval_seconds)
        try:
            page = self._get(
                result_url,
                headers={"User-Agent": "Mozilla/5.0 (compatible; CityLeadPlatform/1.0)"},
            )
            source_url = str(page.url)
            page_html = page.text
            page_text = _visible_text(page.text)
        except httpx.HTTPError:
            source_url = result_url
            page_html = ""
            page_text = ""
        text = f"{result['snippet']} {page_text}"
        keyword_core = re.sub(r"公司$", "", keyword.strip())
        evidence_text = f"{result['title']} {text}"
        if keyword not in evidence_text and (len(keyword_core) < 2 or keyword_core not in evidence_text):
            return None
        mobile = next(iter(MOBILE_PATTERN.findall(text)), None)
        email = next(iter(EMAIL_PATTERN.findall(text)), None)
        wechat_match = WECHAT_PATTERN.search(text)
        wechat = wechat_match.group(1) if wechat_match else None
        if not (mobile or email or wechat) and page_html:
            link_parser = _ContactLinkParser(source_url)
            link_parser.feed(page_html)
            for contact_url in link_parser.links[:3]:
                if self.request_interval_seconds:
                    time.sleep(self.request_interval_seconds)
                try:
                    contact_page = self._get(
                        contact_url,
                        headers={"User-Agent": "Mozilla/5.0 (compatible; CityLeadPlatform/1.0)"},
                    )
                except httpx.HTTPError:
                    continue
                contact_text = _visible_text(contact_page.text)
                mobile = next(iter(MOBILE_PATTERN.findall(contact_text)), None)
                email = next(iter(EMAIL_PATTERN.findall(contact_text)), None)
                wechat_match = WECHAT_PATTERN.search(contact_text)
                wechat = wechat_match.group(1) if wechat_match else None
                if mobile or email or wechat:
                    source_url = str(contact_page.url)
                    page_text = f"{page_text} {contact_text}"
                    break
        if not (mobile or email or wechat):
            return None
        company_name = clean_company_name(result["title"])
        if not company_name:
            return None
        service_intro = select_service_intro(
            result["snippet"],
            page_text,
            contacts=[mobile, email, wechat],
            company_name=company_name,
            keyword=keyword,
        )
        return {
            "company_name": company_name,
            "mobile": mobile,
            "email": email,
            "wechat": wechat,
            "address": "",
            "service_intro": service_intro,
            "source_url": source_url,
            "matched_keyword": keyword,
            "source_confidence": 50,
        }

    def collect(self, *, city: str, keyword: str, request_text: str) -> list[dict[str, Any]]:
        del request_text
        result_rows: list[tuple[str, dict[str, str]]] = []
        search_errors = 0
        city_short = city.removesuffix("市")
        queries = (
            f"{city} {keyword} 公司 官网 联系方式",
            f"{city_short} {keyword} 手机",
            f"{city_short} {keyword} 业务咨询 手机",
            f"{city_short} {keyword} 联系人 手机号码",
            f"{city_short} {keyword} 邮箱 @",
            f"{city_short} {keyword} 微信号",
            f"{city_short} {keyword} 联系我们",
            f"{city_short} {keyword} 企业名录 联系方式",
        )
        for query in queries:
            query_request_succeeded = False
            for page_index in range(self.max_search_pages):
                page_had_results = False
                sources = (
                    (self.search_url, {"query": query, "page": page_index + 1}),
                    (
                        "https://www.so.com/s",
                        {"q": query, "pn": page_index + 1},
                    ),
                    (
                        "https://cn.bing.com/search",
                        {"q": query, "first": page_index * 10 + 1},
                    ),
                )
                for source_url, params in sources:
                    if self.request_interval_seconds:
                        time.sleep(self.request_interval_seconds)
                    try:
                        candidate_response = self._get(
                            source_url,
                            params=params,
                            headers={"User-Agent": "Mozilla/5.0 (compatible; CityLeadPlatform/1.0)"},
                        )
                    except httpx.HTTPError:
                        continue
                    query_request_succeeded = True
                    parser = _SearchResultParser()
                    parser.feed(candidate_response.text)
                    if not parser.results:
                        continue
                    page_had_results = True
                    search_results = parser.results
                    selected_results = list(search_results[: self.max_results])
                    for result in search_results[self.max_results :]:
                        snippet = result["snippet"]
                        if (
                            MOBILE_PATTERN.search(snippet)
                            or EMAIL_PATTERN.search(snippet)
                            or WECHAT_PATTERN.search(snippet)
                        ):
                            selected_results.append(result)
                    result_rows.extend((str(candidate_response.url), result) for result in selected_results)
                if not page_had_results:
                    break
            if not query_request_succeeded:
                search_errors += 1
        if search_errors == len(queries):
            raise ProviderError("public web search failed")
        if not result_rows:
            raise ProviderError("public web search returned no structured results")

        candidate_rows: list[tuple[str, dict[str, str]]] = []
        seen_result_urls: set[str] = set()
        for search_page_url, result in result_rows:
            result_url = urljoin(search_page_url, result["url"])
            parsed_url = urlparse(result_url)
            if (
                parsed_url.scheme not in {"http", "https"}
                or not parsed_url.netloc
                or result_url in seen_result_urls
            ):
                continue
            seen_result_urls.add(result_url)
            candidate_rows.append((search_page_url, result))

        with ThreadPoolExecutor(max_workers=self.detail_workers) as executor:
            collected = executor.map(
                lambda row: self._lead_from_result(row[0], row[1], keyword),
                candidate_rows,
            )

        leads: list[dict[str, Any]] = []
        seen_sources: set[str] = set()
        for lead in collected:
            if lead is None or lead["source_url"] in seen_sources:
                continue
            seen_sources.add(lead["source_url"])
            leads.append(lead)
        return leads
