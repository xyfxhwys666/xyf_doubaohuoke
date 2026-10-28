import re
from collections.abc import Iterable
from datetime import datetime
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

MOBILE_PATTERN = re.compile(r"1[3-9]\d{9}")
EMAIL_PATTERN = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
_PLACEHOLDER_EMAIL_DOMAINS = {
    "example.com",
    "example.cn",
    "example.net",
    "example.org",
    "example.test",
    "invalid",
    "localhost",
}
_FREE_EMAIL_DOMAINS = {
    "gmail.com",
    "outlook.com",
    "hotmail.com",
    "qq.com",
    "163.com",
    "126.com",
    "yeah.net",
    "sina.cn",
    "vip.163.com",
}
_GENERIC_EMAIL_LOCALS = {
    "noreply",
    "no-reply",
    "donotreply",
    "mailer-daemon",
}
NON_COMPANY_TITLE_TERMS = (
    "排行榜",
    "排名",
    "招聘",
    "哪家好",
    "推荐",
    "联系方式",
    "联系电话",
    "黄页",
    "名单",
)
COMPANY_ENTITY_SUFFIXES = (
    "有限公司",
    "股份有限公司",
    "集团有限公司",
    "设计事务所",
    "工作室",
)
SERVICE_BUSINESS_TERMS = (
    "软装",
    "全案",
    "室内",
    "装饰",
    "空间",
    "设计",
    "整装",
    "别墅",
    "家居",
    "家具",
    "陈设",
    "定制",
    "住宅",
)
SERVICE_NOISE_TERMS = (
    "更新时间",
    "更新于",
    "驾车导航",
    "导航",
    "地址",
    "联系电话",
    "联系方式",
    "电话",
    "手机",
    "邮箱",
    "微信",
    "营业时间",
    "查看地图",
    "路线",
)
SERVICE_DATE_PATTERN = re.compile(
    r"(?:19|20)\d{2}(?:年|[-/.])\d{1,2}(?:月|[-/.])\d{1,2}(?:日)?"
)


def china_saved_at() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")


def normalize_mobile(value: object) -> str | None:
    if not value:
        return None
    digits = re.sub(r"\D", "", str(value))
    if len(digits) == 13 and digits.startswith("86"):
        digits = digits[2:]
    return digits if MOBILE_PATTERN.fullmatch(digits) else None


def _email_text_values(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        values: list[str] = []
        for item in value:
            values.extend(_email_text_values(item))
        return values
    return [str(value)]


def _email_source_hosts(source_urls: Iterable[object]) -> set[str]:
    hosts: set[str] = set()
    for raw_url in source_urls:
        parsed = urlparse(str(raw_url or "").strip())
        host = (parsed.hostname or "").lower().removeprefix("www.")
        if host:
            hosts.add(host)
    return hosts


def _email_company_tokens(company_name: object) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]{3,}", str(company_name or "").casefold())
        if token not in {"design", "studio", "深圳", "有限公司"}
    }


def _email_score(
    email: str,
    *,
    company_name: object,
    source_hosts: set[str],
    evidence_text: str,
) -> int:
    local, domain = email.rsplit("@", 1)
    score = 0
    if domain in _PLACEHOLDER_EMAIL_DOMAINS or domain.endswith(".example"):
        score -= 100
    if domain in source_hosts or any(
        domain.endswith(f".{host}") or host.endswith(f".{domain}")
        for host in source_hosts
    ):
        score += 80
    company_tokens = _email_company_tokens(company_name)
    if any(token in domain or token in local for token in company_tokens):
        score += 25
    if email in evidence_text:
        score += 20
    if domain in _FREE_EMAIL_DOMAINS:
        score -= 8
    if local in _GENERIC_EMAIL_LOCALS:
        score -= 10
    return score


def normalize_emails(
    value: object,
    *,
    company_name: object = "",
    source_urls: Iterable[object] = (),
    evidence_quotes: Iterable[object] = (),
) -> list[str]:
    """Return at most two evidence-backed, plausibly real email addresses."""
    candidates: list[str] = []
    for text in _email_text_values(value):
        for match in EMAIL_PATTERN.findall(text):
            normalized = match.strip(" \t\r\n.,;；、()[]{}<>\"").lower()
            if normalized and normalized not in candidates:
                candidates.append(normalized)
    if not candidates:
        return []
    source_hosts = _email_source_hosts(source_urls)
    evidence_text = " ".join(str(value or "") for value in evidence_quotes).casefold()
    ranked = sorted(
        enumerate(candidates),
        key=lambda item: (
            -_email_score(
                item[1],
                company_name=company_name,
                source_hosts=source_hosts,
                evidence_text=evidence_text,
            ),
            item[0],
        ),
    )
    return [email for _, email in ranked[:2]]


def normalize_email(value: object) -> str | None:
    """Return the strongest single email for legacy consumers."""
    emails = normalize_emails(value)
    return emails[0] if emails else None


def limit_lead_emails(lead: dict[str, Any]) -> bool:
    """Normalize a persisted lead's email field in place and report changes."""
    source_urls = [
        lead.get("source_url"),
        *(lead.get("contact_evidence_urls") or []),
        *(lead.get("company_name_evidence_urls") or []),
    ]
    evidence_quotes = [
        lead.get("candidate_reason"),
        lead.get("reason"),
        lead.get("contact_evidence_quote"),
        *(lead.get("contact_evidence_quotes") or []),
        *(lead.get("evidence_quotes") or []),
    ]
    selected = normalize_emails(
        lead.get("email"),
        company_name=lead.get("company_name"),
        source_urls=source_urls,
        evidence_quotes=evidence_quotes,
    )
    normalized = "; ".join(selected) or None
    if lead.get("email") == normalized:
        return False
    lead["email"] = normalized
    return True


def normalize_wechat(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def clean_service_intro(value: object) -> str:
    """Keep business descriptions while dropping search-result boilerplate."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return ""
    fragments = re.split(r"[。！？；;\n]+", text)
    kept: list[str] = []
    for fragment in fragments:
        for clause in re.split(r"[，,|]+", fragment):
            clause = clause.strip(" \t\r\n-—_:：")
            if not clause or any(term in clause for term in SERVICE_NOISE_TERMS):
                continue
            clause = SERVICE_DATE_PATTERN.sub("", clause).strip(" \t\r\n-—_:：")
            if re.search(r"1[3-9]\d{9}|https?://|www\.", clause, re.IGNORECASE):
                continue
            if not any(term in clause for term in SERVICE_BUSINESS_TERMS):
                continue
            kept.append(clause)
    result: list[str] = []
    for fragment in kept:
        if fragment not in result:
            result.append(fragment)
    return "；".join(result)[:280]


def normalize_company_name(value: object) -> str | None:
    name = re.sub(r"\s+", "", str(value or "")).strip("-_|·,，。;；:：")
    if len(name) < 4 or any(term in name for term in NON_COMPANY_TITLE_TERMS):
        return None
    return name


def is_complete_company_entity_name(value: object) -> bool:
    name = normalize_company_name(value)
    return bool(name and name.endswith(COMPANY_ENTITY_SUFFIXES))


def _candidate_information_score(candidate: dict[str, Any]) -> tuple[int, int, int]:
    contact_fields = ("mobile", "email", "wechat", "contact_person_name", "contact_person_role")
    detail_fields = ("address", "service_intro", "source_url", "reason", "matched_keyword")
    evidence_fields = ("source_urls", "evidence_urls", "contact_evidence_urls", "company_name_evidence_urls")
    return (
        sum(bool(candidate.get(field)) for field in contact_fields),
        sum(bool(candidate.get(field)) for field in detail_fields),
        sum(bool(candidate.get(field)) for field in evidence_fields),
    )


def _merge_company_candidate_fields(
    primary: dict[str, Any],
    secondary: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(primary)
    for field in (
        "mobile",
        "email",
        "wechat",
        "contact_person_name",
        "contact_person_role",
        "address",
        "service_intro",
        "source_url",
        "reason",
        "matched_keyword",
        "discovery_channel",
        "contact_evidence_url",
        "contact_evidence_quote",
    ):
        if not merged.get(field) and secondary.get(field):
            merged[field] = secondary[field]
    for field in (
        "source_urls",
        "evidence_urls",
        "contact_evidence_urls",
        "contact_evidence_quotes",
        "company_name_evidence_urls",
    ):
        values: list[Any] = []
        for candidate in (primary, secondary):
            value = candidate.get(field)
            if isinstance(value, (list, tuple, set)):
                values.extend(value)
            elif value:
                values.append(value)
        if values:
            merged[field] = list(dict.fromkeys(values))
    emails = normalize_emails(
        [primary.get("email"), secondary.get("email")],
        company_name=merged.get("company_name"),
        source_urls=[
            merged.get("source_url"),
            *(merged.get("contact_evidence_urls") or []),
            *(merged.get("company_name_evidence_urls") or []),
        ],
        evidence_quotes=[
            merged.get("reason"),
            merged.get("contact_evidence_quote"),
            *(merged.get("contact_evidence_quotes") or []),
        ],
    )
    merged["email"] = "; ".join(emails) or None
    return merged


def deduplicate_company_candidates(
    candidates: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    key_order: list[tuple[str, str]] = []
    for candidate in candidates:
        name = normalize_company_name(candidate.get("company_name"))
        if not name:
            continue
        city = str(candidate.get("city") or "").strip()
        normalized = {**candidate, "city": city, "company_name": name}
        key = (city.casefold(), name.casefold())
        current = by_key.get(key)
        if current is None:
            by_key[key] = normalized
            key_order.append(key)
            continue
        current_confidence = int(current.get("source_confidence") or 0)
        normalized_confidence = int(normalized.get("source_confidence") or 0)
        if normalized_confidence > current_confidence or (
            normalized_confidence == current_confidence
            and _candidate_information_score(normalized)
            > _candidate_information_score(current)
        ):
            primary, secondary = normalized, current
        else:
            primary, secondary = current, normalized
        by_key[key] = _merge_company_candidate_fields(primary, secondary)
    return [by_key[key] for key in key_order]


def normalize_lead(
    raw: dict[str, Any], *, city: str, provider: str, keyword: str | None = None
) -> dict[str, Any]:
    source_url = str(raw.get("source_url") or "").strip() or None
    source_confidence = int(raw.get("source_confidence") or 50)
    evidence_urls = [
        str(value).strip()
        for value in raw.get("contact_evidence_urls") or []
        if str(value).strip()
    ]
    evidence_quotes = [
        str(value).strip()
        for value in raw.get("contact_evidence_quotes") or []
        if str(value).strip()
    ]
    normalized = {
        "city": city,
        "company_name": str(raw.get("company_name") or "").strip(),
        "mobile": normalize_mobile(raw.get("mobile")),
        "contact_person_name": str(raw.get("contact_person_name") or "").strip() or None,
        "contact_person_role": str(raw.get("contact_person_role") or "").strip() or None,
        "email": "; ".join(
            normalize_emails(
                raw.get("email"),
                company_name=raw.get("company_name"),
                source_urls=[
                    source_url,
                    *evidence_urls,
                    raw.get("company_name_evidence_url"),
                ],
                evidence_quotes=[
                    raw.get("reason"),
                    raw.get("candidate_reason"),
                    raw.get("contact_evidence_quote"),
                    *evidence_quotes,
                ],
            )
        )
        or None,
        "wechat": normalize_wechat(raw.get("wechat")),
        "address": str(raw.get("address") or "").strip(),
        "service_intro": clean_service_intro(raw.get("service_intro")),
        "saved_at": china_saved_at(),
        "discovery_channel": provider,
        "source_url": source_url,
        "contact_verification_status": str(
            raw.get("contact_verification_status") or "unverified"
        ),
        "contact_evidence_url": str(
            raw.get("contact_evidence_url") or source_url or ""
        ).strip()
        or None,
        "contact_evidence_quote": str(raw.get("contact_evidence_quote") or "").strip(),
        "contact_evidence_urls": evidence_urls,
        "contact_evidence_quotes": evidence_quotes,
        "contact_evidence_grade": str(raw.get("contact_evidence_grade") or "C"),
        "contact_evidence_score": max(
            0, min(100, int(raw.get("contact_evidence_score") or 0))
        ),
        "contact_verified_at": raw.get("contact_verified_at"),
        "contact_verification_reason": str(
            raw.get("contact_verification_reason") or ""
        ).strip(),
        "matched_keyword": str(raw.get("matched_keyword") or keyword or "").strip(),
        "source_confidence": source_confidence,
        "confidence_score": source_confidence,
        "confidence_level": "待验证",
        "verification_status": "unverified",
        "verification_models": [],
        "verification_detail": {},
        "company_name_verification_status": "unverified",
        "company_name_evidence_urls": [],
        "company_name_verification_reason": "",
        "company_name_verified_at": None,
        "company_name_verification_attempts": 0,
    }
    if evidence_urls and not normalized.get("contact_evidence_url"):
        normalized["contact_evidence_url"] = evidence_urls[0]
    if evidence_quotes and not normalized.get("contact_evidence_quote"):
        normalized["contact_evidence_quote"] = evidence_quotes[0]
    return normalized


def _contact_keys(lead: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    if mobile := normalize_mobile(lead.get("mobile")):
        keys.add(f"mobile:{mobile}")
    if email := normalize_email(lead.get("email")):
        keys.add(f"email:{email}")
    if wechat := normalize_wechat(lead.get("wechat")):
        keys.add(f"wechat:{wechat.lower()}")
    company_name = normalize_company_name(lead.get("company_name"))
    if company_name:
        city = str(lead.get("city") or "").strip().casefold()
        keys.add(f"company:{city}:{company_name.casefold()}")
    return keys


def deduplicate_leads(leads: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    owners: dict[str, dict[str, Any]] = {}
    for lead in leads:
        keys = _contact_keys(lead)
        matches: list[dict[str, Any]] = []
        for key in keys:
            owner = owners.get(key)
            if owner is not None and all(owner is not item for item in matches):
                matches.append(owner)
        if not matches:
            result.append(lead)
            for key in keys:
                owners[key] = lead
            continue

        target = matches[0]
        candidates = [target, *matches[1:], lead]
        richest = max(
            candidates,
            key=lambda item: (
                int(item.get("contact_evidence_score") or 0),
                item.get("contact_verification_status")
                in {"verified_official_page", "verified_cross_source", "verified_public_web"},
                sum(bool(item.get(field)) for field in ("mobile", "email", "wechat")),
                bool(item.get("source_url")),
            ),
        )
        emails = normalize_emails(
            [item.get("email") for item in candidates],
            company_name=target.get("company_name"),
            source_urls=[item.get("source_url") for item in candidates],
            evidence_quotes=[
                quote
                for item in candidates
                for quote in (
                    [item.get("contact_evidence_quote")]
                    + list(item.get("contact_evidence_quotes") or [])
                )
            ],
        )
        target["email"] = "; ".join(emails) or None
        for field in ("wechat", "address", "service_intro"):
            if not target.get(field):
                target[field] = next((item.get(field) for item in candidates if item.get(field)), target.get(field))
        contact_bundle_fields = (
            "mobile",
            "contact_person_name",
            "contact_person_role",
            "contact_verification_status",
            "contact_evidence_url",
            "contact_evidence_quote",
            "contact_evidence_urls",
            "contact_evidence_quotes",
            "contact_evidence_grade",
            "contact_evidence_score",
            "contact_verified_at",
            "contact_verification_reason",
        )
        if any(richest.get(field) for field in contact_bundle_fields):
            for field in contact_bundle_fields:
                target[field] = richest.get(field)
        for field in (
            "source_url",
            "discovery_channel",
            "verification_status",
            "verification_models",
            "verification_detail",
            "matched_keyword",
            "source_confidence",
            "confidence_score",
            "confidence_level",
            "company_name_verification_status",
            "company_name_evidence_urls",
            "company_name_verification_reason",
            "company_name_verified_at",
            "company_name_verification_attempts",
        ):
            if richest.get(field):
                target[field] = richest[field]
        for duplicate in matches[1:]:
            result[:] = [item for item in result if item is not duplicate]
        merged_keys = set().union(*(_contact_keys(item) for item in candidates))
        for key in merged_keys:
            owners[key] = target
    return result
