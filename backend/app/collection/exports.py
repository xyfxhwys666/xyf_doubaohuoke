import csv
import json
import re
from collections.abc import Iterable
from io import BytesIO, StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal, TextIO
from zipfile import ZIP_DEFLATED, ZipFile

from openpyxl import Workbook

RESULT_FIELDS = [
    "city",
    "company_name",
    "mobile",
    "contact_person_name",
    "contact_person_role",
    "contact_verification_status",
    "contact_evidence_url",
    "contact_evidence_urls",
    "contact_evidence_quote",
    "contact_verified_at",
    "contact_verification_reason",
    "email",
    "wechat",
    "address",
    "service_intro",
    "saved_at",
    "discovery_channel",
    "source_url",
    "matched_keyword",
    "source_confidence",
    "confidence_score",
    "confidence_level",
    "verification_status",
    "verification_models",
    "verification_detail",
    "company_name_verification_status",
    "company_name_evidence_urls",
    "company_name_verification_reason",
    "company_name_verified_at",
    "company_name_verification_attempts",
    "delivery_status",
    "delivery_error",
    "delivered_at",
]
UNPUBLISHED = "未公开"


def _safe_filename(city: str) -> str:
    return re.sub(r"[^\w.-]", "_", city, flags=re.UNICODE).strip(".") or "city"


def _csv_row(lead: dict[str, Any]) -> dict[str, Any]:
    return {
        **lead,
        "verification_models": ",".join(lead.get("verification_models") or []),
        "company_name_evidence_urls": ",".join(
            lead.get("company_name_evidence_urls") or []
        ),
        "contact_evidence_urls": ",".join(lead.get("contact_evidence_urls") or []),
        "verification_detail": json.dumps(lead.get("verification_detail") or {}, ensure_ascii=False),
        **{
            field: lead.get(field) or UNPUBLISHED
            for field in (
                "mobile",
                "contact_person_name",
                "contact_person_role",
                "contact_evidence_url",
                "contact_evidence_quote",
                "contact_verified_at",
                "contact_verification_reason",
                "email",
                "wechat",
                "address",
                "service_intro",
            )
        },
        "contact_verification_status": lead.get("contact_verification_status")
        or "未验证",
    }


def _csv_bytes(leads: Iterable[dict[str, Any]]) -> bytes:
    stream = StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=RESULT_FIELDS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(_csv_row(lead) for lead in leads)
    return ("\ufeff" + stream.getvalue()).encode("utf-8")


def _xlsx_bytes(leads: Iterable[dict[str, Any]]) -> bytes:
    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet()
    sheet.title = "Leads"
    sheet.append(RESULT_FIELDS)
    for lead in leads:
        sheet.append(
            [
                ",".join(lead.get(field) or [])
                if field
                in {
                    "verification_models",
                    "company_name_evidence_urls",
                    "contact_evidence_urls",
                }
                else json.dumps(lead.get(field) or {}, ensure_ascii=False)
                if field == "verification_detail"
                else lead.get(field) or UNPUBLISHED
                for field in RESULT_FIELDS
            ]
        )
    stream = BytesIO()
    workbook.save(stream)
    return stream.getvalue()


def _json_bytes(leads: Iterable[dict[str, Any]]) -> bytes:
    stream = StringIO()
    stream.write("[\n")
    for index, lead in enumerate(leads):
        row = {
            field: (lead.get(field) if lead.get(field) not in (None, "", [], {}) else UNPUBLISHED)
            for field in RESULT_FIELDS
        }
        if index:
            stream.write(",\n")
        stream.write(json.dumps(row, ensure_ascii=False, indent=2))
    stream.write("\n]\n")
    return stream.getvalue().encode("utf-8")


def _iter_staged_leads(stream: TextIO) -> Iterable[dict[str, Any]]:
    for line in stream:
        yield json.loads(line)


def build_city_zip(
    task: dict[str, Any],
    export_format: Literal["xlsx", "csv", "json"],
    leads: Iterable[dict[str, Any]] | None = None,
) -> bytes:
    stream = BytesIO()
    source = task.get("leads", []) if leads is None else leads
    city_indexes = {city: index for index, city in enumerate(task["cities"])}
    with TemporaryDirectory(prefix="collection-export-") as temporary_dir:
        root = Path(temporary_dir)
        staged_paths: dict[str, Path] = {}
        staged_streams: dict[str, TextIO] = {}
        try:
            for lead in source:
                city = lead.get("city")
                if city not in city_indexes:
                    continue
                if city not in staged_streams:
                    path = root / f"{city_indexes[city]}.jsonl"
                    staged_paths[city] = path
                    staged_streams[city] = path.open("w", encoding="utf-8", newline="\n")
                staged_streams[city].write(json.dumps(lead, ensure_ascii=False) + "\n")
        finally:
            for staged_stream in staged_streams.values():
                staged_stream.close()

        with ZipFile(stream, "w", compression=ZIP_DEFLATED) as archive:
            for city in task["cities"]:
                path = staged_paths.get(city)
                if path is None:
                    continue
                with path.open(encoding="utf-8") as staged_stream:
                    city_leads = _iter_staged_leads(staged_stream)
                    if export_format == "csv":
                        payload = _csv_bytes(city_leads)
                    elif export_format == "json":
                        payload = _json_bytes(city_leads)
                    else:
                        payload = _xlsx_bytes(city_leads)
                archive.writestr(f"{_safe_filename(city)}.{export_format}", payload)
    return stream.getvalue()
