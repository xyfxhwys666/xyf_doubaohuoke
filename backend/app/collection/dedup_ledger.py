from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from uuid import NAMESPACE_URL, uuid5

from app.collection.normalization import normalize_company_name, normalize_email, normalize_mobile


@dataclass(frozen=True, slots=True)
class LedgerDecision:
    action: str
    customer_guid: str
    company_key: str
    mobile: str
    evidence_score: int
    email: str | None = None


class DedupLedger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()

    @staticmethod
    def company_key(city: str, company_name: str) -> str:
        normalized = normalize_company_name(company_name) or company_name.strip()
        return f"{city.strip().casefold()}:{normalized.casefold()}"

    @staticmethod
    def _guid(company_key: str) -> str:
        return str(uuid5(NAMESPACE_URL, f"city-lead:{company_key}"))

    def _load(self) -> dict[str, dict[str, dict[str, object]]]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {"companies": {}, "mobiles": {}, "emails": {}}
        if not isinstance(value, dict):
            return {"companies": {}, "mobiles": {}, "emails": {}}
        companies = value.get("companies")
        mobiles = value.get("mobiles")
        emails = value.get("emails")
        return {
            "companies": companies if isinstance(companies, dict) else {},
            "mobiles": mobiles if isinstance(mobiles, dict) else {},
            "emails": emails if isinstance(emails, dict) else {},
        }

    def _write(self, value: dict[str, object]) -> None:
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def evaluate(
        self,
        *,
        city: str,
        company_name: str,
        mobile: str | None = None,
        email: str | None = None,
        evidence_score: int,
    ) -> LedgerDecision:
        normalized_mobile = normalize_mobile(mobile)
        normalized_email = normalize_email(email)
        if not normalized_mobile and not normalized_email:
            raise ValueError("a valid mainland mobile or email is required")
        company_key = self.company_key(city, company_name)
        score = max(0, min(100, int(evidence_score)))
        with self._lock:
            state = self._load()
            mobile_owner = state["mobiles"].get(normalized_mobile)
            email_owner = state["emails"].get(normalized_email) if normalized_email else None
            company_owner = state["companies"].get(company_key)
        if isinstance(mobile_owner, dict) and mobile_owner.get("company_key") != company_key:
            return LedgerDecision(
                "skip_duplicate_mobile",
                str(mobile_owner["customer_guid"]),
                company_key,
                normalized_mobile,
                score,
                normalized_email,
            )
        if isinstance(email_owner, dict) and email_owner.get("company_key") != company_key:
            return LedgerDecision(
                "skip_duplicate_email",
                str(email_owner["customer_guid"]),
                company_key,
                normalized_mobile or "",
                score,
                normalized_email,
            )
        if isinstance(company_owner, dict):
            action = (
                "update"
                if score > int(company_owner.get("evidence_score") or 0)
                else "skip_duplicate_company"
            )
            return LedgerDecision(
                action,
                str(company_owner["customer_guid"]),
                company_key,
                normalized_mobile or "",
                score,
                normalized_email,
            )
        return LedgerDecision(
            "create",
            self._guid(company_key),
            company_key,
            normalized_mobile or "",
            score,
            normalized_email,
        )

    def mark_delivered(self, decision: LedgerDecision) -> None:
        if decision.action not in {"create", "update"}:
            return
        with self._lock:
            state = self._load()
            record = {
                "customer_guid": decision.customer_guid,
                "mobile": decision.mobile,
                "email": decision.email,
                "evidence_score": decision.evidence_score,
                "delivered_at": datetime.now(UTC).isoformat(),
            }
            state["companies"][decision.company_key] = record
            if decision.mobile:
                state["mobiles"][decision.mobile] = {
                    "customer_guid": decision.customer_guid,
                    "company_key": decision.company_key,
                }
            if decision.email:
                state["emails"][decision.email] = {
                    "customer_guid": decision.customer_guid,
                    "company_key": decision.company_key,
                }
            self._write(state)

    def is_company_delivered(self, city: str, company_name: str) -> bool:
        """Return whether this city/company is already recorded as ERP-delivered."""
        key = self.company_key(city, company_name)
        with self._lock:
            return key in self._load()["companies"]
