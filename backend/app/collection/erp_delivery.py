from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import httpx

from app.collection.dedup_ledger import DedupLedger, LedgerDecision


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _captured_at(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return datetime.now(UTC).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    try:
        parsed = datetime.fromisoformat(text)
        return parsed.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return text.replace("T", " ")[:19]


def _contact_type(lead: dict[str, Any]) -> str:
    labels = [
        label
        for field, label in (
            ("mobile", "手机号"),
            ("email", "邮箱"),
            ("wechat", "微信号"),
        )
        if lead.get(field)
    ]
    return "、".join(labels) or "其他联系方式"


def build_erp_payload(task: dict[str, Any], lead: dict[str, Any]) -> dict[str, Any]:
    city = str(lead.get("city") or "").strip()
    company_name = str(lead.get("company_name") or "").strip()
    normalized_name = str(
        lead.get("normalized_company_name") or company_name.casefold()
    ).strip()
    guid = str(uuid5(NAMESPACE_URL, f"city-lead:{city}:{normalized_name}"))
    other_parts = []
    if address := str(lead.get("address") or "").strip():
        other_parts.append(f"地址：{address}")
    if source_url := str(lead.get("source_url") or "").strip():
        other_parts.append(f"来源：{source_url}")
    contact_info = {
        "contactInfoType": _contact_type(lead),
        **({"mobilePhone": str(lead["mobile"])} if lead.get("mobile") else {}),
        **({"email": str(lead["email"])} if lead.get("email") else {}),
        **({"wechatId": str(lead["wechat"])} if lead.get("wechat") else {}),
        **({"otherContact": "；".join(other_parts)} if other_parts else {}),
    }
    return {
        "sourceAccountName": "豆包",
        "customerAccountGuid": guid,
        "customerSourceChannel": "豆包",
        "customerCapturedAt": _captured_at(lead.get("saved_at")),
        "customerAccountName": company_name,
        "customerProfileBio": str(lead.get("service_intro") or ""),
        "customerIpRegion": city,
        "customerType": "B端客资",
        "cEndCustomerRequirement": str(task.get("request_text") or ""),
        "sourceMethod": "AI联网采集",
        "contactInfo": contact_info,
    }


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    status: str
    customer_guid: str
    error: str | None = None
    fingerprint: str | None = None


class ErpLeadDelivery:
    def __init__(
        self,
        data_dir: Path,
        target_url: str | None,
        *,
        timeout_seconds: float = 30.0,
        client: httpx.Client | None = None,
        bearer_token: str | None = None,
    ) -> None:
        self.target_url = target_url
        self.bearer_token = bearer_token
        self.delivery_dir = data_dir / "delivery"
        self.failures_path = self.delivery_dir / "failures.json"
        self.delivered_path = self.delivery_dir / "delivered.json"
        self.dedup_ledger = DedupLedger(self.delivery_dir / "dedup-ledger.json")
        self.delivery_dir.mkdir(parents=True, exist_ok=True)
        self._client = client or httpx.Client(
            timeout=timeout_seconds,
            follow_redirects=True,
        )
        self._lock = RLock()

    @property
    def configured(self) -> bool:
        return bool(self.target_url)

    @staticmethod
    def _fingerprint(payload: dict[str, Any]) -> str:
        stable_payload = {
            key: value
            for key, value in payload.items()
            if key != "customerCapturedAt"
        }
        encoded = json.dumps(
            stable_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def fingerprint(self, task: dict[str, Any], lead: dict[str, Any]) -> str:
        return self._fingerprint(build_erp_payload(task, lead))

    def is_company_delivered(self, city: str, company_name: str) -> bool:
        return self.dedup_ledger.is_company_delivered(city, company_name)

    @staticmethod
    def _failure_key(task_id: str, fingerprint: str) -> str:
        return f"{task_id}:{fingerprint}"

    @staticmethod
    def _load(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _write(path: Path, value: dict[str, Any]) -> None:
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)

    @classmethod
    def _migrate_legacy_failures(cls, failures: dict[str, Any]) -> bool:
        migrated = False
        for key, item in list(failures.items()):
            if not isinstance(item, dict) or item.get("customer_guid"):
                continue
            payload = item.get("payload")
            if not isinstance(payload, dict):
                continue
            guid = str(payload.get("customerAccountGuid") or key)
            task_id = str(item.get("task_id") or "")
            fingerprint = str(item.get("fingerprint") or "")
            if not task_id or not fingerprint:
                continue
            failure_key = cls._failure_key(task_id, fingerprint)
            failures.setdefault(failure_key, {**item, "customer_guid": guid})
            if failure_key != key:
                failures.pop(key)
            migrated = True
        return migrated

    def _post(
        self,
        payload: dict[str, Any],
        *,
        target_url: str | None = None,
        bearer_token: str | None = None,
    ) -> str | None:
        endpoint = target_url if target_url is not None else self.target_url
        if not endpoint:
            return "ERP automatic delivery is not configured"
        headers = {"Content-Type": "application/json; charset=utf-8"}
        token = bearer_token if bearer_token is not None else self.bearer_token
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            response = self._client.post(
                endpoint,
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            return str(exc)
        if not isinstance(body, dict):
            return "ERP returned an invalid JSON object"
        if body.get("errCode") != 0:
            return str(body.get("errMsg") or f"ERP errCode={body.get('errCode')}")
        return None

    def deliver(
        self,
        task: dict[str, Any],
        lead: dict[str, Any],
        *,
        target_url: str | None = None,
        bearer_token: str | None = None,
    ) -> DeliveryResult:
        payload = build_erp_payload(task, lead)
        guid = payload["customerAccountGuid"]
        fingerprint = self._fingerprint(payload)
        task_id = str(task.get("id") or "")
        failure_key = self._failure_key(task_id, fingerprint)
        endpoint = target_url if target_url is not None else self.target_url
        if not endpoint:
            return DeliveryResult("disabled", guid, fingerprint=fingerprint)
        with self._lock:
            mobile = str(lead.get("mobile") or "")
            email = str(lead.get("email") or "")
            if mobile or email:
                decision = self.dedup_ledger.evaluate(
                    city=str(lead.get("city") or ""),
                    company_name=str(lead.get("company_name") or ""),
                    mobile=mobile,
                    email=email,
                    evidence_score=int(lead.get("contact_evidence_score") or 0),
                )
                guid = decision.customer_guid
                payload["customerAccountGuid"] = guid
                fingerprint = self._fingerprint(payload)
                failure_key = self._failure_key(task_id, fingerprint)
                if decision.action.startswith("skip_"):
                    return DeliveryResult("skipped", guid, fingerprint=fingerprint)
            else:
                decision = None
            delivered = self._load(self.delivered_path)
            if delivered.get(guid, {}).get("fingerprint") == fingerprint:
                return DeliveryResult("skipped", guid, fingerprint=fingerprint)
            failures = self._load(self.failures_path)
            migrated = self._migrate_legacy_failures(failures)
            previous = failures.get(failure_key, {})
            if previous.get("fingerprint") == fingerprint:
                if migrated:
                    self._write(self.failures_path, failures)
                return DeliveryResult(
                    "failed",
                    guid,
                    previous.get("last_error"),
                    fingerprint,
                )
            error = self._post(
                payload,
                target_url=endpoint,
                bearer_token=bearer_token,
            )
            if error is None:
                failures.pop(failure_key, None)
                delivered[guid] = {
                    "task_id": task_id,
                    "fingerprint": fingerprint,
                    "delivered_at": _now(),
                }
                self._write(self.failures_path, failures)
                self._write(self.delivered_path, delivered)
                if decision is not None:
                    self.dedup_ledger.mark_delivered(decision)
                return DeliveryResult("completed", guid, fingerprint=fingerprint)
            delivered.pop(guid, None)
            failures[failure_key] = {
                "task_id": task_id,
                "customer_guid": guid,
                "payload": payload,
                "fingerprint": fingerprint,
                "attempts": int(previous.get("attempts") or 0) + 1,
                "last_error": error,
                "last_attempt_at": _now(),
                "ledger_decision": (
                    {
                        "action": decision.action,
                        "customer_guid": decision.customer_guid,
                        "company_key": decision.company_key,
                        "mobile": decision.mobile,
                        "email": decision.email,
                        "evidence_score": decision.evidence_score,
                    }
                    if decision is not None
                    else None
                ),
            }
            self._write(self.failures_path, failures)
            self._write(self.delivered_path, delivered)
        return DeliveryResult("failed", guid, error, fingerprint)

    def failure_counts(self, task_id: str | None = None) -> dict[str, int]:
        with self._lock:
            failures = self._load(self.failures_path)
        if task_id is not None:
            failures = {
                guid: item
                for guid, item in failures.items()
                if item.get("task_id") == task_id
            }
        return {"pending": len(failures)}

    def retry_results(self, task_id: str | None = None) -> list[DeliveryResult]:
        with self._lock:
            failures = self._load(self.failures_path)
            if self._migrate_legacy_failures(failures):
                self._write(self.failures_path, failures)
            selected = [
                (failure_key, item)
                for failure_key, item in failures.items()
                if task_id is None or item.get("task_id") == task_id
            ]
            results: list[DeliveryResult] = []
            for failure_key, item in selected:
                error = self._post(item["payload"])
                current = failures.get(failure_key)
                if current is None:
                    continue
                guid = str(
                    current.get("customer_guid")
                    or current.get("payload", {}).get("customerAccountGuid")
                    or failure_key
                )
                if error is None:
                    failures.pop(failure_key, None)
                    delivered = self._load(self.delivered_path)
                    delivered[guid] = {
                        "task_id": current.get("task_id", ""),
                        "fingerprint": current.get("fingerprint", ""),
                        "delivered_at": _now(),
                    }
                    self._write(self.delivered_path, delivered)
                    raw_decision = current.get("ledger_decision")
                    if isinstance(raw_decision, dict):
                        self.dedup_ledger.mark_delivered(
                            LedgerDecision(
                                action=str(raw_decision["action"]),
                                customer_guid=str(raw_decision["customer_guid"]),
                                company_key=str(raw_decision["company_key"]),
                                mobile=str(raw_decision.get("mobile") or ""),
                                evidence_score=int(raw_decision["evidence_score"]),
                                email=str(raw_decision.get("email") or "") or None,
                            )
                        )
                    results.append(
                        DeliveryResult(
                            "completed",
                            guid,
                            fingerprint=str(current.get("fingerprint") or ""),
                        )
                    )
                else:
                    current["attempts"] = int(current.get("attempts") or 0) + 1
                    current["last_error"] = error
                    current["last_attempt_at"] = _now()
                    results.append(
                        DeliveryResult(
                            "failed",
                            guid,
                            error,
                            str(current.get("fingerprint") or ""),
                        )
                    )
                self._write(self.failures_path, failures)
        return results

    def retry(self, task_id: str | None = None) -> dict[str, int]:
        results = self.retry_results(task_id)
        return {
            "attempted": len(results),
            "succeeded": sum(result.status == "completed" for result in results),
            "failed": sum(result.status == "failed" for result in results),
        }
