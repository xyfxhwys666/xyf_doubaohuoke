import re
from dataclasses import dataclass
from typing import Literal

from app.collection.cities import tier3_plus_city_names

ContactField = Literal["mobile", "email", "wechat"]
ContactMatch = Literal["any", "all"]

KEYWORD_PATTERN = re.compile(
    r"全案软装公司|全案设计公司|软装设计公司|软装公司|整案公司|整装公司|设计公司"
)
_RESIDENTIAL_SCOPE_PATTERN = re.compile(
    r"别墅|豪宅|大宅|私宅|高端住宅|住宅|家装|居住"
)
_IRRELEVANT_KEYWORD_PATTERN = re.compile(
    r"工业|产品|平面|品牌|广告|视觉|包装|景观|园林|建材|材料|招聘|培训|供应商"
)
_RESIDENTIAL_KEYWORD_EXPANSIONS = (
    "别墅软装设计公司",
    "别墅全案设计公司",
    "别墅室内设计公司",
    "别墅装修设计公司",
    "别墅整装公司",
    "高端住宅软装公司",
    "高端住宅设计公司",
    "高端住宅室内设计公司",
    "豪宅软装公司",
    "豪宅设计公司",
    "私宅设计公司",
    "大宅设计公司",
    "住宅软装设计公司",
    "住宅全案设计公司",
    "住宅室内设计公司",
    "室内装饰设计公司",
    "空间设计公司",
    "家居陈设设计公司",
    "软装陈设公司",
    "全屋软装公司",
    "全案落地设计公司",
    "整案设计公司",
    "整装设计公司",
)
CONTACT_TERMS: tuple[tuple[ContactField, tuple[str, ...]], ...] = (
    ("mobile", ("手机号码", "手机号", "手机", "电话号码", "电话")),
    ("email", ("邮箱号码", "邮箱", "邮件", "email")),
    ("wechat", ("微信号码", "微信号", "微信", "wechat")),
)
ALL_MATCH_TERMS = ("都要", "全部具备", "全部都要", "缺一不可", "同时具备", "全都要")


@dataclass(slots=True)
class ParsedCollectionIntent:
    cities: list[str]
    all_cities: bool
    keywords: list[str]
    models: list[str]
    required_contacts: list[ContactField]
    contact_match: ContactMatch
    limit_per_city: int | None


def _extract_cities(text: str) -> list[str]:
    result = []
    for city in tier3_plus_city_names():
        alias = city.removesuffix("市")
        if city in text or alias in text:
            result.append(city)
    return result


def _extract_keywords(text: str) -> list[str]:
    keywords = list(dict.fromkeys(KEYWORD_PATTERN.findall(text)))
    if keywords:
        return keywords
    customer_match = re.search(r"(?:寻找|找|查询|查)([^，。；\n]{2,24}?)(?:客户|企业|，|。|；|$)", text)
    candidate = customer_match.group(1).strip() if customer_match else ""
    return [candidate] if candidate else ["软装公司"]


def _expand_keywords(text: str, keywords: list[str], model_keywords: list[str]) -> list[str]:
    """Expand residential intent deterministically while removing unsafe noise."""
    residential_scope = bool(_RESIDENTIAL_SCOPE_PATTERN.search(text))
    combined = [*keywords, *model_keywords]
    filtered: list[str] = []
    for keyword in combined:
        cleaned = str(keyword).strip()
        if not cleaned or _IRRELEVANT_KEYWORD_PATTERN.search(cleaned):
            continue
        if residential_scope and cleaned == "设计公司":
            continue
        if cleaned not in filtered:
            filtered.append(cleaned)
    if residential_scope:
        for keyword in _RESIDENTIAL_KEYWORD_EXPANSIONS:
            if keyword not in filtered:
                filtered.append(keyword)
    return filtered[:24]


def _extract_models(text: str, configured_models: set[str]) -> list[str]:
    lowered = text.lower()
    requested = []
    if "deepseek" in lowered or "deep seek" in lowered or "深度求索" in text:
        requested.append("deepseek")
    if "豆包" in text or "doubao" in lowered:
        requested.append("doubao")
    available = [model_id for model_id in requested if model_id in configured_models]
    if available:
        return available
    if "doubao" in configured_models:
        return ["doubao"]
    if "deepseek" in configured_models:
        return ["deepseek"]
    return []


def _extract_contacts(text: str) -> list[ContactField]:
    lowered = text.lower()
    contacts = [field for field, terms in CONTACT_TERMS if any(term in lowered for term in terms)]
    return contacts or ["mobile"]


def _extract_limit(text: str) -> int | None:
    match = re.search(
        r"(?:只要|最多|限定|限制|查|找|得到|获得)?\s*(\d{1,4})\s*"
        r"(?:家|条(?:有效)?(?:客户)?(?:信息|线索)?|个(?:有效)?(?:客户|公司|线索)?)",
        text,
    )
    if not match:
        return None
    return max(1, min(1000, int(match.group(1))))


def parse_collection_intent(
    request_text: str,
    *,
    configured_models: set[str],
    model_keywords: list[str] | None = None,
) -> ParsedCollectionIntent:
    text = request_text.strip()
    cities = _extract_cities(text)
    contacts = _extract_contacts(text)
    keywords = _expand_keywords(text, _extract_keywords(text), model_keywords or [])
    contact_match: ContactMatch = (
        "all" if len(contacts) == 1 or any(term in text for term in ALL_MATCH_TERMS) else "any"
    )
    return ParsedCollectionIntent(
        cities=cities,
        all_cities=not cities,
        keywords=keywords,
        models=_extract_models(text, configured_models),
        required_contacts=contacts,
        contact_match=contact_match,
        limit_per_city=_extract_limit(text),
    )
