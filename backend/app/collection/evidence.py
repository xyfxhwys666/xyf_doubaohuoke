from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from urllib.parse import urlparse

from app.collection.normalization import normalize_company_name, normalize_mobile


class EvidenceGrade(StrEnum):
    A = "A"
    B = "B"
    C = "C"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class ContactEvidence:
    company_name: str
    mobile: str
    grade: EvidenceGrade
    score: int
    auto_valid: bool
    independent_source_count: int
    sources: tuple[dict[str, Any], ...]


def _compact(value: object) -> str:
    return "".join(str(value or "").casefold().split())


def _domain(url: object) -> str:
    host = urlparse(str(url or "")).netloc.casefold().split(":", 1)[0]
    return host.removeprefix("www.")


def grade_contact_evidence(
    company_name: str,
    mobile: str,
    sources: Iterable[dict[str, Any]],
    *,
    aliases: Iterable[str] = (),
) -> ContactEvidence:
    normalized_company = normalize_company_name(company_name) or company_name.strip()
    normalized_mobile = normalize_mobile(mobile) or ""
    source_items = tuple(dict(item) for item in sources)
    if not normalized_mobile or any(item.get("conflicting_company") for item in source_items):
        return ContactEvidence(
            normalized_company,
            normalized_mobile,
            EvidenceGrade.REJECTED,
            0,
            False,
            0,
            source_items,
        )

    names = {_compact(normalized_company), *(_compact(alias) for alias in aliases)}
    names.discard("")
    matching: list[dict[str, Any]] = []
    for item in source_items:
        quote = _compact(item.get("quote"))
        domain = _domain(item.get("url"))
        if not domain or normalized_mobile not in quote:
            continue
        if any(name in quote for name in names):
            matching.append(item)

    domains = {_domain(item.get("url")) for item in matching}
    domains.discard("")
    authoritative = any(item.get("authoritative") for item in matching)
    has_forward = any(item.get("stage") in {"legal_name", "alias", "forward"} for item in matching)
    has_reverse = any(item.get("stage") == "reverse_mobile" for item in matching)
    if authoritative:
        grade = EvidenceGrade.A
        score = 100
    elif has_forward and has_reverse and len(domains) >= 2:
        grade = EvidenceGrade.B
        score = min(95, 80 + len(domains) * 5)
    else:
        grade = EvidenceGrade.C
        score = min(60, 20 + len(domains) * 10)
    return ContactEvidence(
        normalized_company,
        normalized_mobile,
        grade,
        score,
        grade in {EvidenceGrade.A, EvidenceGrade.B},
        len(domains),
        source_items,
    )


def choose_best_contact(contacts: Iterable[ContactEvidence]) -> ContactEvidence:
    values = list(contacts)
    if not values:
        raise ValueError("at least one contact is required")
    return max(
        values,
        key=lambda item: (
            item.score,
            item.independent_source_count,
            item.mobile,
        ),
    )
