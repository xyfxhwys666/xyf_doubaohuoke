from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse, urlunparse

_MAINLAND_MOBILE = re.compile(r"^1[3-9]\d{9}$")
_MOBILE_IN_TEXT = re.compile(
    r"(?<!\d)1[3-9](?:[\s\-‐‑–—]?\d){9}(?!\d)"
)
_URL = re.compile(r"https?://[^\s｜|,，;；]+", re.IGNORECASE)
_NOT_FOUND = ("未找到可核验公开手机号", "未公开", "无可核验联系方式")
_DISCOVERY_EXCLUSION_PATTERN = re.compile(
    r"工业设计|工业产品|产品设计|平面设计|品牌设计|广告设计|视觉设计|包装设计|"
    r"景观设计|园林设计|招聘|求职|培训|建材|建筑材料|家具供应|材料供应|供应商"
)
_RESIDENTIAL_EVIDENCE_PATTERN = re.compile(
    r"别墅|住宅|私宅|豪宅|大宅|软装|陈设|全案|整案|整装|室内"
)


class DoubaoWebResponseError(ValueError):
    """Raised when visible Doubao output cannot be parsed safely."""


@dataclass(frozen=True, slots=True)
class DoubaoWebCandidate:
    company_name: str
    city: str
    matched_keyword: str
    source_urls: list[str]
    evidence_quotes: list[str]
    raw_response: str


@dataclass(frozen=True, slots=True)
class DoubaoWebContact:
    company_name: str
    mobile: str | None
    contact_person_name: str | None
    contact_person_role: str | None
    email: str | None
    wechat: str | None
    address: str | None
    service_intro: str | None
    source_urls: list[str]
    evidence_quotes: list[str]
    raw_response: str


def build_discovery_prompt(*, city: str, keywords: list[str], request_text: str) -> str:
    keyword_text = "、".join(item.strip() for item in keywords if item.strip())
    return (
        f"请联网查找{city}符合“{request_text}”的{keyword_text}，最多5家。\n"
        "不得编造公司名、来源或原文；不要把登录页当证据。\n"
        "只保留在目标城市实际经营、主营别墅/高端住宅/私宅/住宅室内/软装/全案的公司；"
        "不要输出与住宅、别墅或软装无关的同行。工业设计、产品设计、平面/品牌/广告/视觉设计、"
        "景观/园林设计、办公空间、招聘培训、建材材料供应商和只有目录文章而非公司主体的结果一律排除。\n"
        "按上述正常业务关键词、城市行政区和公开项目案例完成公司检索；不要以 GEO、SEO 或投放为检索词，"
        "也不要为了这些信号单独发起查询。若正常查到的来源原文顺带明确提到近6个月的平台内容投放或软文推广，"
        "只把它作为排序信号；不得把投放GEO当作事实；查不到这类记录时按原有公司搜索逻辑继续，不得编造或补全。\n"
        "只输出JSON数组，不要输出说明、Markdown表格或代码围栏。\n"
        "每项字段：company_name、matched_keyword、source_urls、evidence_quotes。\n"
        "找不到可核验来源的公司不要输出。"
    )


def build_contact_prompt(
    *, company_name: str, city: str, requested_contacts: list[str]
) -> str:
    contact_text = "、".join(requested_contacts) or "mobile"
    company_query = company_name.strip()
    if city.strip() and not company_query.startswith(city.strip()):
        company_query = f"{city.strip()}{company_query}"
    return (
        f"请联网只查询这1家公司：{company_query}。\n"
        f"查找公开的{contact_text}，手机号必须是中国大陆11位号码，不要座机或400号码。\n"
        "可使用公司官网、企业信息页面、行业目录、地图页面或搜索结果摘要等公开来源。\n"
        "如果联网结果中确实出现中国大陆手机号，先返回联网结果中实际出现的手机号，系统会再次核验；"
        "来源暂时无法打开时不要编造来源，可保留已看到的号码并将source_urls和evidence_quotes留空。\n"
        "如果无法按JSON输出，直接用自然语言写出原文中同时出现的公司全称和手机号，不要因此拒绝回答。\n"
        "只填写联网结果中实际出现的号码，不得根据手机号、公司名称或常识推测或补全。\n"
        "联系人优先返回完整姓名；只有公开原文明确写出姓氏加先生或女士时，才可按原文返回。\n"
        "联系人姓名和职务不得根据手机号、公司名称或常识推测，未公开或原文没有就填null。\n"
        "只输出JSON，不要输出说明、Markdown或代码围栏。字段为："
        "company_name、mobile、contact_person_name、contact_person_role、email、wechat、"
        "address、service_intro、source_urls、evidence_quotes。\n"
        "source_urls和evidence_quotes必须逐字支持公司全称与手机号；联系人和职务有公开原文再填写，"
        "没有就填null。若没有公开可对应的手机号，请返回：未找到可核验公开手机号。"
    )


def parse_discovery_response(
    text: str,
    *,
    city: str,
    keywords: list[str] | None = None,
) -> list[DoubaoWebCandidate]:
    raw = text.strip()
    if not raw:
        raise DoubaoWebResponseError("豆包返回为空")

    payload = _json_payload(raw)
    rows = _discovery_json_rows(payload) if payload is not None else _pipe_rows(raw)
    result: list[DoubaoWebCandidate] = []
    for row in rows:
        if isinstance(row, dict):
            company_name = _clean(row.get("company_name") or row.get("公司名称"))
            matched_keyword = _clean(row.get("matched_keyword") or row.get("匹配关键词"))
            urls = _normalize_urls(row.get("source_urls") or row.get("source_url"))
            quotes = _clean_list(row.get("evidence_quotes") or row.get("evidence_quote"))
        else:
            parts = _split_pipe(row)
            if len(parts) < 3:
                continue
            company_name = _clean(re.sub(r"^\s*\d+[.、．)]?\s*", "", parts[0]))
            matched_keyword = ""
            urls = _normalize_urls(parts[1])
            quotes = _clean_list(parts[2])
        if not company_name or not urls or not quotes:
            continue
        evidence_text = " ".join((company_name, matched_keyword, *quotes))
        if _DISCOVERY_EXCLUSION_PATTERN.search(evidence_text):
            continue
        if (
            re.search(r"办公空间|办公室设计", evidence_text)
            and not _RESIDENTIAL_EVIDENCE_PATTERN.search(evidence_text)
        ):
            continue
        result.append(
            DoubaoWebCandidate(
                company_name=company_name,
                city=city.strip(),
                matched_keyword=matched_keyword,
                source_urls=urls,
                evidence_quotes=quotes,
                raw_response=raw,
            )
        )
    if not result:
        raise DoubaoWebResponseError("豆包返回中没有可解析的公司证据")
    return result


def parse_contact_response(text: str, *, company_name: str) -> DoubaoWebContact:
    raw = text.strip()
    if not raw:
        raise DoubaoWebResponseError("豆包返回为空")
    if any(marker in raw for marker in _NOT_FOUND) and not _URL.search(raw):
        return _empty_contact(company_name.strip(), raw)

    payload = _json_payload(raw)
    if isinstance(payload, list):
        payload = payload[0] if payload else None
    if isinstance(payload, dict):
        parsed_name = _clean(payload.get("company_name") or payload.get("公司名称"))
        urls = _normalize_urls(payload.get("source_urls") or payload.get("source_url"))
        quotes = _clean_list(payload.get("evidence_quotes") or payload.get("evidence_quote"))
        values = payload
    else:
        rows = _pipe_rows(raw)
        parts = _split_pipe(rows[0]) if rows else []
        if len(parts) < 4:
            # The web chat sometimes ignores the JSON instruction and answers
            # in a sentence. Accept that form only when the same response
            # explicitly contains the requested company and a valid mobile;
            # a phone number on its own is not enough to create a lead.
            if _company_key(company_name) not in _company_key(raw):
                raise DoubaoWebResponseError("豆包联系方式返回格式无法解析")
            natural_mobile = _extract_mobile(raw)
            if natural_mobile is None:
                raise DoubaoWebResponseError("豆包自然语言回答中没有有效手机号")
            parsed_name = company_name.strip()
            urls = _normalize_urls(raw)
            quotes = []
            values = {
                "mobile": natural_mobile,
                "contact_person_name": _labeled_value(raw, "联系人", "联系人姓名"),
                "contact_person_role": _labeled_value(raw, "职务", "职位"),
                "email": _labeled_value(raw, "邮箱", "电子邮箱", "email"),
                "wechat": _labeled_value(raw, "微信", "微信号", "wechat"),
                "address": _labeled_value(raw, "公司地址", "地址"),
                "service_intro": _labeled_value(
                    raw,
                    "服务简介",
                    "服务介绍",
                    "公司简介",
                    "简介",
                ),
            }
        else:
            parsed_name = _clean(parts[0])
            urls = _normalize_urls(parts[2])
            quotes = _clean_list(parts[3])
            values = {"mobile": parts[1]}

    mobile = _normalize_mobile(values.get("mobile") or values.get("手机号"))
    if mobile is None:
        for quote in quotes:
            mobile = _extract_mobile(quote)
            if mobile:
                break
    # Keep a valid number returned by the model visible even when the response
    # has no citations yet.  The service marks it unverified and the ERP gate
    # still rejects it until independent evidence is found.
    if not _company_names_match(company_name, parsed_name):
        # Doubao often changes only punctuation or returns a short brand name.
        # Accept that response only when the requested full name and the same
        # phone are present in the cited evidence; otherwise keep the hard gate.
        evidence_text = " ".join(quotes)
        target_in_evidence = _company_key(company_name) in _company_key(evidence_text)
        if not (target_in_evidence and mobile and urls) and not _company_alias_matches(
            company_name,
            parsed_name,
        ):
            raise DoubaoWebResponseError("豆包返回的公司名与请求不匹配")
    return DoubaoWebContact(
        company_name=company_name.strip(),
        mobile=mobile,
        contact_person_name=_optional(values, "contact_person_name", "联系人"),
        contact_person_role=_optional(values, "contact_person_role", "职务"),
        email=_optional(values, "email", "邮箱"),
        wechat=_optional(values, "wechat", "微信"),
        address=_optional(values, "address", "地址"),
        service_intro=_optional(values, "service_intro", "服务介绍"),
        source_urls=urls,
        evidence_quotes=quotes,
        raw_response=raw,
    )


def _json_payload(text: str) -> Any | None:
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.IGNORECASE | re.DOTALL)
    candidate = fenced.group(1).strip() if fenced else text
    if not candidate.startswith(("[", "{")):
        return None
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise DoubaoWebResponseError("豆包 JSON 返回无法解析") from exc


def _labeled_value(text: str, *labels: str) -> str | None:
    """Extract only an explicitly labelled value from a natural-language reply."""
    label_pattern = "|".join(
        re.escape(label) for label in sorted(labels, key=len, reverse=True)
    )
    match = re.search(
        rf"(?:{label_pattern})\s*[：:]\s*([^；;。\n]+)",
        text,
        re.IGNORECASE,
    )
    return _clean(match.group(1)) if match else None


def _discovery_json_rows(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("companies", "candidates", "data", "results"):
            if isinstance(payload.get(key), list):
                return payload[key]
        return [payload]
    return []


def _pipe_rows(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if "｜" in line or "|" in line]


def _split_pipe(row: str) -> list[str]:
    parts = [part.strip() for part in re.split(r"[｜|]", row)]
    if parts and not parts[0]:
        parts = parts[1:]
    if parts and not parts[-1]:
        parts = parts[:-1]
    if parts and re.fullmatch(r"\d+[.、．)]?", parts[0]):
        parts = parts[1:]
    return parts


def _normalize_urls(value: object) -> list[str]:
    values = value if isinstance(value, list) else _URL.findall(str(value or ""))
    result: list[str] = []
    for item in values:
        parsed = urlparse(str(item).strip())
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            continue
        normalized = urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/", "", parsed.query, ""))
        if normalized not in result:
            result.append(normalized)
    return result


def _clean_list(value: object) -> list[str]:
    values = value if isinstance(value, list) else [value]
    result: list[str] = []
    for item in values:
        cleaned = _clean(item)
        if cleaned and cleaned not in {"未提供来源", "未公开", "null"} and cleaned not in result:
            result.append(cleaned)
    return result


def _normalize_mobile(value: object) -> str | None:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 13 and digits.startswith("86"):
        digits = digits[2:]
    return digits if _MAINLAND_MOBILE.fullmatch(digits) else None


def _extract_mobile(value: object) -> str | None:
    for match in _MOBILE_IN_TEXT.finditer(str(value or "")):
        mobile = _normalize_mobile(match.group())
        if mobile:
            return mobile
    return None


def _clean(value: object) -> str:
    return str(value or "").strip()


def _company_key(value: object) -> str:
    return re.sub(r"[\s\W_]+", "", _clean(value)).casefold()


def _company_alias_matches(requested: object, parsed: object) -> bool:
    """Accept a clear brand/short-name variant for later evidence checking."""
    requested_key = _company_key(requested)
    parsed_key = _company_key(parsed)
    if not requested_key or not parsed_key:
        return False
    if len(parsed_key) < 4:
        return False
    return parsed_key in requested_key or requested_key in parsed_key


def _company_names_match(requested: object, parsed: object) -> bool:
    requested_key = _company_key(requested)
    parsed_key = _company_key(parsed)
    return bool(requested_key and parsed_key and (
        requested_key == parsed_key or _company_alias_matches(requested, parsed)
    ))


def _optional(values: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = _clean(values.get(key))
        if value and value.casefold() not in {"null", "none"} and value not in {"未公开", "未找到"}:
            return value
    return None


def _empty_contact(company_name: str, raw: str) -> DoubaoWebContact:
    return DoubaoWebContact(
        company_name=company_name,
        mobile=None,
        contact_person_name=None,
        contact_person_role=None,
        email=None,
        wechat=None,
        address=None,
        service_intro=None,
        source_urls=[],
        evidence_quotes=[],
        raw_response=raw,
    )
