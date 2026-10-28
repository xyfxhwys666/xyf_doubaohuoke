import re
from typing import Any
from urllib.parse import urlparse

ROUND_CANDIDATE_LIMIT = 100
DEFAULT_VALID_LEAD_TARGET = 100
ROUND_COOLDOWN_SECONDS = 600
TARGET_ROUND_COOLDOWN_SECONDS = 30
MATCHED_VERIFICATION_STATUSES = {
    "cross_verified",
    "single_model_match",
    "model_consensus_match",
}
VERIFIED_COMPANY_NAME_STATUSES = {
    "verified_public_web",
    "verified_official_page",
    "verified_cross_source",
}
VERIFIED_EVIDENCE_STATUSES = {
    "verified_public_web",
    "verified_official_page",
    "verified_cross_source",
}
_MOBILE_PATTERN = re.compile(r"^1[3-9]\d{9}$")
_EMAIL_PATTERN = re.compile(
    r"^[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}$", re.IGNORECASE
)
_EMAIL_TOKEN_PATTERN = re.compile(
    r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE
)
_EVIDENCE_BACKED_TARGET_MARKERS = (
    "手机号或邮箱任一即可",
    "手机号或邮箱任意一个就行",
    "手机或邮箱任意一个",
    "公开手机号或公开邮箱之一",
)


def new_round_metrics() -> dict[str, int]:
    return {
        "candidates": 0,
        "new_companies": 0,
        "verification_matches": 0,
        "verification_rejects": 0,
        "erp_succeeded": 0,
        "erp_failed": 0,
    }


def rotate_keywords(keywords: list[str], round_number: int) -> list[str]:
    if not keywords:
        return []
    offset = (max(1, round_number) - 1) % len(keywords)
    return keywords[offset:] + keywords[:offset]


def candidate_pool_is_sufficient(candidate_count: int, candidate_target: int) -> bool:
    """Return whether a round can focus on enriching existing candidates."""
    return candidate_target > 0 and candidate_count >= candidate_target


def discovery_query_groups(
    keywords: list[str], *, round_number: int
) -> list[list[str]]:
    original = list(dict.fromkeys(value.strip() for value in keywords if value.strip()))
    groups: list[list[str]] = [original] if original else []
    text = " ".join(original)
    expansions: list[list[str]] = []
    if "别墅" in text or "私宅" in text or "大宅" in text:
        expansions.append(
            [
                "私宅设计公司",
                "大宅设计公司",
                "豪宅设计公司",
                "高端住宅设计公司",
                "室内设计公司",
                "空间设计公司",
            ]
        )
    if "软装" in text or "陈设" in text:
        expansions.append(["软装陈设公司", "全屋软装公司", "软装设计公司"])
    if "全案" in text or "整案" in text or "整装" in text:
        expansions.append(
            ["全案落地公司", "整案设计公司", "整装设计公司", "整装落地公司"]
        )
    seen = set(original)
    for group in expansions:
        unique = [value for value in group if value not in seen]
        if not unique:
            continue
        seen.update(unique)
        groups.append(unique)
    if len(groups) > 1:
        offset = (max(1, round_number) - 1) % (len(groups) - 1)
        groups[1:] = groups[1 + offset :] + groups[1 : 1 + offset]
    return groups


def verified_erp_ineligibility(lead: dict[str, Any]) -> str | None:
    if (
        lead.get("company_name_verification_status")
        not in VERIFIED_COMPANY_NAME_STATUSES
    ):
        return "company_name_unverified"

    if lead.get("contact_verification_status") not in VERIFIED_EVIDENCE_STATUSES:
        return "contact_unverified"

    mobile = re.sub(r"\D", "", str(lead.get("mobile") or ""))
    if not _MOBILE_PATTERN.fullmatch(mobile):
        return "missing_mobile"

    source_url = str(lead.get("contact_evidence_url") or "").strip()
    parsed_source = urlparse(source_url)
    if parsed_source.scheme not in {"http", "https"} or not parsed_source.netloc:
        return "missing_source_url"

    if lead.get("contact_verification_status") in {
        "verified_public_web",
        "verified_official_page",
    }:
        return None

    if lead.get("verification_status") not in MATCHED_VERIFICATION_STATUSES:
        return "verification_not_matched"
    return None


def verified_delivery_ineligibility(lead: dict[str, Any]) -> str | None:
    """Return the reason a verified lead cannot be sent to ERP.

    ERP accepts either a normalized mainland mobile or a normalized email. The
    legacy phone-only eligibility rule remains available for older ERP callers.
    """
    if lead.get("company_name_verification_status") not in VERIFIED_COMPANY_NAME_STATUSES:
        return "company_name_unverified"
    if lead.get("contact_verification_status") not in VERIFIED_EVIDENCE_STATUSES:
        return "contact_unverified"
    mobile = re.sub(r"\D", "", str(lead.get("mobile") or ""))
    email = str(lead.get("email") or "").strip()
    valid_emails = _EMAIL_TOKEN_PATTERN.findall(email)
    if not _MOBILE_PATTERN.fullmatch(mobile) and not any(
        _EMAIL_PATTERN.fullmatch(value) for value in valid_emails
    ):
        return "missing_mobile_or_email"
    source_url = str(lead.get("contact_evidence_url") or "").strip()
    parsed_source = urlparse(source_url)
    if parsed_source.scheme not in {"http", "https"} or not parsed_source.netloc:
        return "missing_source_url"
    if lead.get("contact_verification_status") in {
        "verified_public_web",
        "verified_official_page",
    }:
        return None
    if lead.get("verification_status") not in MATCHED_VERIFICATION_STATUSES:
        return "verification_not_matched"
    return None


def strict_verified_lead_count(leads: list[dict[str, Any]]) -> int:
    return sum(verified_delivery_ineligibility(lead) is None for lead in leads)


def _has_public_contact(lead: dict[str, Any]) -> bool:
    mobile = re.sub(r"\D", "", str(lead.get("mobile") or ""))
    if _MOBILE_PATTERN.fullmatch(mobile):
        return True
    email = str(lead.get("email") or "")
    return any(_EMAIL_PATTERN.fullmatch(value) for value in _EMAIL_TOKEN_PATTERN.findall(email))


def _has_public_source(lead: dict[str, Any]) -> bool:
    candidates: list[Any] = [
        lead.get("contact_evidence_url"),
        lead.get("source_url"),
        *(lead.get("contact_evidence_urls") or []),
    ]
    for value in candidates:
        parsed = urlparse(str(value or "").strip())
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            return True
    return False


def evidence_backed_contact_count(leads: list[dict[str, Any]]) -> int:
    """Count usable phone/email rows while retaining a public source requirement."""
    return sum(_has_public_contact(lead) and _has_public_source(lead) for lead in leads)


def target_contact_mode(task: dict[str, Any]) -> str:
    """Return the explicit or request-derived completion mode for a task."""
    explicit_mode = str(task.get("target_contact_mode") or "").strip().casefold()
    request_text = str(task.get("request_text") or "")
    if explicit_mode in {"evidence_backed", "contact_or_evidence"}:
        return "evidence_backed"
    if any(
        marker in request_text for marker in _EVIDENCE_BACKED_TARGET_MARKERS
    ):
        return "evidence_backed"
    return "strict"


def target_contact_count(task: dict[str, Any]) -> int:
    """Return the completion count required by a continuous task's request."""
    if target_contact_mode(task) == "evidence_backed":
        return evidence_backed_contact_count(task.get("leads", []))
    return strict_verified_lead_count(task.get("leads", []))
