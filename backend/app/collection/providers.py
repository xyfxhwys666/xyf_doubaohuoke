import re
import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Protocol
from urllib.parse import urlencode

import httpx

from app.config import Settings

MOBILE_PATTERN = re.compile(r"1[3-9]\d{9}")


class CollectionProvider(Protocol):
    id: str
    label: str
    configured: bool

    def list_cities(self) -> list[str]: ...

    def collect(self, *, city: str, keyword: str, request_text: str) -> list[dict[str, Any]]: ...


class ProviderError(RuntimeError):
    pass


def _text(value: object) -> str:
    if isinstance(value, list):
        return ";".join(str(item) for item in value if item)
    return str(value or "").strip()


@dataclass(slots=True)
class AmapProvider:
    api_key: str | None
    base_url: str = "https://restapi.amap.com"
    max_pages: int = 3
    request_interval_seconds: float = 0.3
    retry_limit: int = 3
    client: httpx.Client | None = None
    id: str = "amap"
    label: str = "高德地图"
    _request_lock: Lock = field(default_factory=Lock, init=False, repr=False)
    _last_request_at: float = field(default=0, init=False, repr=False)

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _get(self, path: str, params: dict[str, object]) -> dict[str, Any]:
        if not self.api_key:
            raise ProviderError("provider amap is not configured")
        request_params = {**params, "key": self.api_key}
        for attempt in range(self.retry_limit + 1):
            try:
                with self._request_lock:
                    wait_seconds = self.request_interval_seconds - (time.monotonic() - self._last_request_at)
                    if wait_seconds > 0:
                        time.sleep(wait_seconds)
                    if self.client is not None:
                        response = self.client.get(f"{self.base_url.rstrip('/')}{path}", params=request_params)
                    else:
                        response = httpx.get(
                            f"{self.base_url.rstrip('/')}{path}",
                            params=request_params,
                            timeout=30,
                            follow_redirects=True,
                        )
                    self._last_request_at = time.monotonic()
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                if attempt < self.retry_limit:
                    time.sleep(max(self.request_interval_seconds, 0.5 * (attempt + 1)))
                    continue
                raise ProviderError("amap request failed") from exc
            if payload.get("status") == "1":
                return payload
            info = str(payload.get("info") or "unknown error")
            if "EXCEEDED_THE_LIMIT" in info and attempt < self.retry_limit:
                time.sleep(max(self.request_interval_seconds, 0.5 * (attempt + 1)))
                continue
            raise ProviderError(f"amap error: {info}")
        raise ProviderError("amap retry limit exhausted")

    def list_cities(self) -> list[str]:
        payload = self._get(
            "/v3/config/district",
            {"keywords": "中国", "subdistrict": 2, "extensions": "base"},
        )
        names: set[str] = set()

        def visit(node: dict[str, Any]) -> None:
            if node.get("level") == "city" and _text(node.get("name")):
                names.add(_text(node["name"]))
                return
            for child in node.get("districts") or []:
                if isinstance(child, dict):
                    visit(child)

        for district in payload.get("districts") or []:
            if isinstance(district, dict):
                visit(district)
        if not names:
            raise ProviderError("amap returned no prefecture-level cities")
        return sorted(names)

    def collect(self, *, city: str, keyword: str, request_text: str) -> list[dict[str, Any]]:
        del request_text
        leads: list[dict[str, Any]] = []
        for page in range(1, self.max_pages + 1):
            payload = self._get(
                "/v3/place/text",
                {
                    "keywords": keyword,
                    "city": city,
                    "citylimit": "true",
                    "extensions": "all",
                    "offset": 25,
                    "page": page,
                },
            )
            pois = payload.get("pois") or []
            for poi in pois:
                if not isinstance(poi, dict):
                    continue
                mobile_match = MOBILE_PATTERN.search(_text(poi.get("tel")))
                email = _text(poi.get("email")) or None
                name = _text(poi.get("name"))
                location = _text(poi.get("location"))
                if not (mobile_match or email) or not name or not location:
                    continue
                source_url = "https://uri.amap.com/marker?" + urlencode(
                    {
                        "position": location,
                        "name": name,
                        "src": "city-lead-platform",
                        "coordinate": "gaode",
                        "callnative": "0",
                    }
                )
                leads.append(
                    {
                        "company_name": name,
                        "mobile": mobile_match.group(0) if mobile_match else None,
                        "email": email,
                        "wechat": None,
                        "address": _text(poi.get("address")),
                        "service_intro": _text(poi.get("type")),
                        "source_url": source_url,
                    }
                )
            total = int(payload.get("count") or 0)
            if not pois or page * 25 >= total:
                break
        return leads


def build_providers(settings: Settings) -> dict[str, CollectionProvider]:
    from app.collection.web_search import WebSearchProvider

    return {
        "web_search": WebSearchProvider(
            max_results=settings.web_search_max_results,
            max_search_pages=settings.web_search_max_pages,
            request_interval_seconds=settings.web_search_request_interval_seconds,
            contact_verification_timeout_seconds=(
                settings.web_search_contact_verification_timeout_seconds
            ),
            max_contact_source_urls=settings.web_search_max_contact_source_urls,
        ),
    }
