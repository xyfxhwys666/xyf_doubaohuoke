import json
import re
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.collection.evidence import choose_best_contact, grade_contact_evidence
from app.collection.normalization import (
    is_complete_company_entity_name,
    normalize_company_name,
    normalize_mobile,
)
from app.config import Settings


class ModelVerificationError(RuntimeError):
    pass


class ModelAccountError(ModelVerificationError):
    pass


def _company_search_aliases(company_name: str, city: str) -> list[str]:
    provided = re.sub(r"\s+", "", company_name)
    stem = provided
    for suffix in ("股份有限公司", "有限责任公司", "集团有限公司", "有限公司"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    city_stem = re.sub(r"[省市自治区]$", "", city.strip())
    for prefix in (city.strip(), f"{city_stem}市", city_stem):
        if len(prefix) >= 2 and stem.startswith(prefix):
            stem = stem[len(prefix) :]
            break
    aliases = [stem]
    for suffix in ("装饰设计", "空间设计", "软装设计", "设计"):
        if stem.endswith(suffix) and len(stem) - len(suffix) >= 2:
            aliases.append(stem[: -len(suffix)])
            break
    return list(dict.fromkeys(alias for alias in aliases if len(alias) >= 2))


class ModelVerifier(Protocol):
    id: str
    label: str
    configured: bool

    def verify(
        self,
        leads: list[dict[str, Any]],
        *,
        request_text: str,
        city: str,
        keyword: str,
    ) -> list[dict[str, Any]]: ...

    def discover(
        self, *, request_text: str, city: str, keyword: str
    ) -> list[dict[str, Any]]: ...

    def discover_companies(
        self,
        *,
        request_text: str,
        city: str,
        keywords: list[str],
        limit: int = 100,
    ) -> list[dict[str, Any]]: ...

    def enrich_company(
        self,
        *,
        request_text: str,
        city: str,
        company: dict[str, Any],
        required_contacts: list[str],
    ) -> dict[str, Any]: ...


@dataclass(slots=True)
class OpenAICompatibleVerifier:
    id: str
    label: str
    api_key: str | None
    base_url: str | None
    model: str | None
    client: httpx.Client | None = None
    required_model_prefix: str | None = None

    @property
    def configured(self) -> bool:
        if not (self.api_key and self.base_url and self.model):
            return False
        return not self.required_model_prefix or self.model.startswith(self.required_model_prefix)

    @property
    def uses_responses_api(self) -> bool:
        return "volces.com" in (self.base_url or "").lower()

    @staticmethod
    def _extract_response_text(payload: dict[str, Any]) -> str:
        direct_text = payload.get("output_text")
        if isinstance(direct_text, str) and direct_text.strip():
            return direct_text
        for output in payload.get("output") or []:
            if not isinstance(output, dict) or output.get("type") != "message":
                continue
            for content in output.get("content") or []:
                if (
                    isinstance(content, dict)
                    and content.get("type") == "output_text"
                    and isinstance(content.get("text"), str)
                    and content["text"].strip()
                ):
                    return content["text"]
        raise KeyError("output_text")

    @staticmethod
    def _extract_web_citations(payload: dict[str, Any]) -> list[dict[str, str]]:
        citations: list[dict[str, str]] = []
        for output in payload.get("output") or []:
            if not isinstance(output, dict):
                continue
            for content in output.get("content") or []:
                if not isinstance(content, dict):
                    continue
                for annotation in content.get("annotations") or []:
                    if not isinstance(annotation, dict) or annotation.get("type") != "url_citation":
                        continue
                    url = str(annotation.get("url") or "").strip()
                    if not url:
                        continue
                    citations.append(
                        {
                            "url": url,
                            "title": str(annotation.get("title") or "").strip(),
                            "summary": str(annotation.get("summary") or "").strip(),
                        }
                    )
        return citations

    @staticmethod
    def _citation_text(citation: dict[str, str]) -> str:
        return re.sub(
            r"\s+",
            "",
            f"{citation.get('title', '')}{citation.get('summary', '')}",
        ).casefold()

    def _request_json(
        self,
        prompt: str,
        *,
        system_prompt: str,
        operation: str,
        use_web_search: bool = False,
    ) -> dict[str, Any]:
        if not self.configured:
            raise ModelVerificationError(f"model {self.id} is not configured")
        requester = self.client or httpx.Client(timeout=120)
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        try:
            if self.uses_responses_api:
                request_payload: dict[str, Any] = {
                    "model": self.model,
                    "input": [
                        {
                            "role": "system",
                            "content": [{"type": "input_text", "text": system_prompt}],
                        },
                        {
                            "role": "user",
                            "content": [{"type": "input_text", "text": prompt}],
                        },
                    ],
                }
                if use_web_search:
                    request_payload.update(
                        {
                            "tools": [{"type": "web_search", "max_keyword": 2, "limit": 10}],
                            "max_tool_calls": 2,
                        }
                    )
                response = requester.post(
                    f"{self.base_url.rstrip('/')}/responses",
                    headers=headers,
                    json=request_payload,
                )
                response.raise_for_status()
                response_payload = response.json()
                content = self._extract_response_text(response_payload)
                citations = self._extract_web_citations(response_payload)
            else:
                response = requester.post(
                    f"{self.base_url.rstrip('/')}/chat/completions",
                    headers=headers,
                    json={
                        "model": self.model,
                        "temperature": 0,
                        "response_format": {"type": "json_object"},
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": prompt},
                        ],
                    },
                )
                response.raise_for_status()
                content = response.json()["choices"][0]["message"]["content"]
                citations = []
            cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.IGNORECASE)
            result = json.loads(cleaned)
            if not isinstance(result, dict):
                raise TypeError("model response must be an object")
            result["_web_citations"] = citations
            return result
        except httpx.HTTPStatusError as exc:
            try:
                error = exc.response.json().get("error") or {}
            except (TypeError, ValueError):
                error = {}
            code = str(error.get("code") or "")
            message = str(error.get("message") or "")
            if exc.response.status_code in {401, 403}:
                detail = f"{code}: {message}".strip(": ")
                raise ModelAccountError(detail or "model account access denied") from exc
            raise ModelVerificationError(f"model {self.id} {operation} failed") from exc
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ModelVerificationError(f"model {self.id} {operation} failed") from exc

    def verify(
        self,
        leads: list[dict[str, Any]],
        *,
        request_text: str,
        city: str,
        keyword: str,
    ) -> list[dict[str, Any]]:
        verdicts: list[dict[str, Any]] = []
        for start in range(0, len(leads), 8):
            verdicts.extend(
                self._verify_batch(
                    leads[start : start + 8],
                    request_text=request_text,
                    city=city,
                    keyword=keyword,
                )
            )
        return verdicts

    def _verify_batch(
        self,
        leads: list[dict[str, Any]],
        *,
        request_text: str,
        city: str,
        keyword: str,
    ) -> list[dict[str, Any]]:
        if not self.configured:
            raise ModelVerificationError(f"model {self.id} is not configured")
        evidence = [
            {
                "index": index,
                "company_name": lead.get("company_name"),
                "mobile": lead.get("mobile"),
                "email": lead.get("email"),
                "wechat": lead.get("wechat"),
                "address": lead.get("address"),
                "service_intro": lead.get("service_intro"),
                "source_url": lead.get("source_url"),
            }
            for index, lead in enumerate(leads)
        ]
        prompt = (
            f"任务需求：{request_text}\n城市：{city}\n关键词：{keyword}\n"
            "请仅依据下方证据判断每家公司是否符合任务需求。不得新增、猜测或修改任何联系方式。"
            "严格返回 JSON：{\"items\":[{\"index\":0,\"relevant\":true,"
            "\"confidence\":85,\"reason\":\"简短理由\"}]}。confidence 必须是 0 到 100。\n"
            f"证据：{json.dumps(evidence, ensure_ascii=False)}"
        )
        items = self._request_json(
            prompt,
            system_prompt="你是企业线索证据核验器，只能依据给定证据判断。",
            operation="verification",
        ).get("items")
        if not isinstance(items, list):
            raise ModelVerificationError(f"model {self.id} verification failed")
        by_index = {int(item["index"]): item for item in items}
        if set(by_index) != set(range(len(leads))):
            raise ModelVerificationError(f"model {self.id} returned incomplete verdicts")
        verdicts = []
        for index in range(len(leads)):
            item = by_index[index]
            relevant = bool(item["relevant"])
            default_confidence = 85 if relevant else 30
            try:
                confidence = round(float(item.get("confidence", default_confidence)))
            except (TypeError, ValueError):
                confidence = default_confidence
            verdicts.append(
                {
                    "relevant": relevant,
                    "confidence": max(0, min(100, confidence)),
                    "reason": str(item.get("reason") or ""),
                }
            )
        return verdicts

    def discover(self, *, request_text: str, city: str, keyword: str) -> list[dict[str, Any]]:
        if not self.configured:
            raise ModelVerificationError(f"model {self.id} is not configured")
        prompt = (
            f"任务需求：{request_text}\n城市：{city}\n关键词：{keyword}\n"
            "请根据你掌握的公开信息寻找该城市符合关键词的 B 端公司。所有结果都会标记为待验证，"
            "可返回你认为约 50% 或以上可能正确的候选，"
            "不要仅因当前无法联网就返回空数组。公司名称必须是你确知存在的真实公司，不确定的公司不要返回。"
            "联系方式不确定可填 null，不要把座机当手机；来源网页记得就填写，不记得可为 null。"
            "最多返回 50 家。严格返回 JSON："
            '{"items":[{"company_name":"公司名","mobile":"手机号或null","email":"邮箱或null",'
            '"wechat":"微信或null","address":"地址","service_intro":"服务定位",'
            '"source_url":"依据网页或null","confidence":70}]}。confidence 必须是 0 到 100。'
        )
        items = self._request_json(
            prompt,
            system_prompt="你是 B2B 客户采集助手，只输出有公开依据的公司信息。",
            operation="discovery",
            use_web_search=True,
        ).get("items")
        if not isinstance(items, list):
            raise ModelVerificationError(f"model {self.id} discovery failed")
        results = []
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                confidence = round(float(item.get("confidence", 70)))
            except (TypeError, ValueError):
                confidence = 70
            results.append(
                {
                    "company_name": item.get("company_name"),
                    "mobile": item.get("mobile"),
                    "email": item.get("email"),
                    "wechat": item.get("wechat"),
                    "address": item.get("address"),
                    "service_intro": item.get("service_intro"),
                    "source_url": item.get("source_url"),
                    "matched_keyword": keyword,
                    "source_confidence": max(0, min(100, confidence)),
                }
            )
        return results

    def discover_companies(
        self,
        *,
        request_text: str,
        city: str,
        keywords: list[str],
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        capped_limit = max(1, min(100, int(limit)))
        prompt = (
            f"任务需求：{request_text}\n城市：{city}\n客户类型关键词："
            f"{json.dumps(keywords, ensure_ascii=False)}\n"
            "第一阶段只返回候选公司，不查询或返回任何联系方式、地址、简介或来源链接。"
            "仅列出你确知真实存在且符合至少一个关键词的公司；不确定、榜单、招聘、文章标题不要返回。"
            f"最多返回 {capped_limit} 家。严格返回 JSON："
            '{"items":[{"company_name":"完整公司名称","matched_keyword":"命中的关键词",'
            '"reason":"候选理由","confidence":70}]}。confidence 必须是 0 到 100。'
        )
        items, citations = self._request_items(
            prompt,
            system_prompt="你是 B2B 公司候选发现助手，只输出真实存在的公司候选。",
            operation="company discovery",
            use_web_search=True,
        )
        results: list[dict[str, Any]] = []
        for item in items[:capped_limit]:
            if not isinstance(item, dict):
                continue
            company_name = str(item.get("company_name") or "").strip()
            normalized_company_name = normalize_company_name(company_name)
            if not normalized_company_name or not is_complete_company_entity_name(
                normalized_company_name
            ):
                continue
            matching_citations = [
                citation
                for citation in citations
                if re.sub(r"\s+", "", normalized_company_name).casefold()
                in self._citation_text(citation)
                and city.removesuffix("市").casefold() in self._citation_text(citation)
            ]
            if self.uses_responses_api and not matching_citations:
                continue
            try:
                confidence = round(float(item.get("confidence", 70)))
            except (TypeError, ValueError):
                confidence = 70
            results.append(
                {
                    "company_name": normalized_company_name,
                    "matched_keyword": str(item.get("matched_keyword") or "").strip(),
                    "reason": str(item.get("reason") or "").strip(),
                    "source_confidence": max(0, min(100, confidence)),
                    **(
                        {
                            "source_url": matching_citations[0]["url"],
                            "company_name_evidence_urls": [
                                citation["url"] for citation in matching_citations
                            ],
                            "company_name_verification_status": "verified_public_web",
                        }
                        if matching_citations
                        else {}
                    ),
                }
            )
        return results

    def enrich_company(
        self,
        *,
        request_text: str,
        city: str,
        company: dict[str, Any],
        required_contacts: list[str],
    ) -> dict[str, Any]:
        company_name = str(company.get("company_name") or "").strip()
        if not company_name:
            raise ModelVerificationError("company name is required for enrichment")
        prompt = (
            f"任务需求：{request_text}\n城市：{city}\n待查询公司：{company_name}\n"
            f"用户关注的联系方式：{json.dumps(required_contacts, ensure_ascii=False)}\n"
            "一次只能查询这一家公司。请联网搜索并直接给出该公司的公开联系方式，优先查找手机号，"
            "同时补全邮箱、微信、地址和服务定位介绍。手机号必须同时返回来源 URL 和包含公司全称与该号码的证据原文。"
            "联系人姓名和职务只能从同一来源原文提取。不得把座机当手机号，不得推测、猜测、组合或编造；"
            "搜索不到同页证据的字段必须返回 null。"
            "严格返回 JSON："
            '{"items":[{"company_name":"原公司名称","mobile":null,'
            '"contact_person_name":null,"contact_person_role":null,"email":null,"wechat":null,'
            '"address":null,"service_intro":null,"source_url":null,"evidence_quote":null,'
            '"confidence":70}]}。'
        )
        items, citations = self._request_items(
            prompt,
            system_prompt="你是企业联系方式联网搜索助手，一次只搜索一家公司并直接返回公开可查信息。",
            operation="company enrichment",
            use_web_search=True,
        )
        if len(items) != 1 or not isinstance(items[0], dict):
            raise ModelVerificationError(f"model {self.id} returned invalid company enrichment")
        item = items[0]
        mobile = normalize_mobile(item.get("mobile"))
        matching_citation = next(
            (
                citation
                for citation in citations
                if re.sub(r"\s+", "", company_name).casefold()
                in self._citation_text(citation)
                and mobile
                and mobile in re.sub(r"\D", "", citation.get("summary") or "")
            ),
            None,
        )
        if self.uses_responses_api and matching_citation is None:
            aliases = [
                str(value).strip()
                for value in company.get("company_aliases") or []
                if str(value).strip()
            ]
            aliases = list(
                dict.fromkeys([*aliases, *_company_search_aliases(company_name, city)])
            )
            alias_contacts: list[tuple[dict[str, Any], Any]] = []
            if aliases:
                alias_prompt = (
                    f"任务需求：{request_text}\n城市：{city}\n完整公司主体：{company_name}\n"
                    f"已知品牌或简称：{json.dumps(aliases, ensure_ascii=False)}\n"
                    "请使用品牌或简称联网查找这家公司的公开中国大陆11位手机号。"
                    "不要返回座机、400号码或无法引用的号码。严格返回 JSON："
                    '{"items":[{"company_name":"完整公司主体","mobile":null,'
                    '"confidence":70}]}。'
                )
                alias_items, alias_citations = self._request_items(
                    alias_prompt,
                    system_prompt="你只搜索指定公司品牌或简称的公开联系方式，并提供联网引用。",
                    operation="company alias enrichment",
                    use_web_search=True,
                )
                candidate_mobiles = list(
                    dict.fromkeys(
                        candidate
                        for alias_item in alias_items
                        if isinstance(alias_item, dict)
                        if (candidate := normalize_mobile(alias_item.get("mobile")))
                    )
                )[:2]
                for candidate_mobile in candidate_mobiles:
                    forward_citations = [
                        citation
                        for citation in alias_citations
                        if candidate_mobile
                        in re.sub(r"\D", "", citation.get("summary") or "")
                        and any(
                            re.sub(r"\s+", "", alias).casefold()
                            in self._citation_text(citation)
                            for alias in aliases
                        )
                    ]
                    if not forward_citations:
                        continue
                    reverse_prompt = (
                        f"反向核验手机号 {candidate_mobile}。完整公司主体：{company_name}；"
                        f"品牌或简称：{json.dumps(aliases, ensure_ascii=False)}。"
                        "请联网确认该手机号是否明确属于同一公司或品牌。若不能确认，mobile 返回 null。"
                        "严格返回 JSON："
                        '{"items":[{"company_name":"完整公司主体","mobile":null,'
                        '"confidence":70}]}。'
                    )
                    reverse_items, reverse_citations = self._request_items(
                        reverse_prompt,
                        system_prompt="你只做手机号归属反向联网核验，不得猜测归属。",
                        operation="reverse mobile verification",
                        use_web_search=True,
                    )
                    reverse_mobile = next(
                        (
                            normalize_mobile(reverse_item.get("mobile"))
                            for reverse_item in reverse_items
                            if isinstance(reverse_item, dict)
                            and normalize_mobile(reverse_item.get("mobile"))
                            == candidate_mobile
                        ),
                        None,
                    )
                    matching_reverse = [
                        citation
                        for citation in reverse_citations
                        if candidate_mobile
                        in re.sub(r"\D", "", citation.get("summary") or "")
                        and (
                            re.sub(r"\s+", "", company_name).casefold()
                            in self._citation_text(citation)
                            or any(
                                re.sub(r"\s+", "", alias).casefold()
                                in self._citation_text(citation)
                                for alias in aliases
                            )
                        )
                    ]
                    if reverse_mobile is None or not matching_reverse:
                        continue
                    evidence_sources = [
                        *(
                            {
                                "url": citation["url"],
                                "quote": citation.get("summary") or "",
                                "stage": "alias",
                            }
                            for citation in forward_citations
                        ),
                        *(
                            {
                                "url": citation["url"],
                                "quote": citation.get("summary") or "",
                                "stage": "reverse_mobile",
                            }
                            for citation in matching_reverse
                        ),
                    ]
                    graded = grade_contact_evidence(
                        company_name,
                        candidate_mobile,
                        evidence_sources,
                        aliases=aliases,
                    )
                    if graded.auto_valid:
                        alias_item = next(
                            value
                            for value in alias_items
                            if isinstance(value, dict)
                            and normalize_mobile(value.get("mobile")) == candidate_mobile
                        )
                        alias_contacts.append((alias_item, graded))
            if alias_contacts:
                selected = choose_best_contact(value for _, value in alias_contacts)
                item = next(value for value, graded in alias_contacts if graded is selected)
                mobile = selected.mobile
                selected_urls = list(
                    dict.fromkeys(
                        str(source.get("url") or "")
                        for source in selected.sources
                        if str(source.get("url") or "")
                    )
                )
                selected_quotes = [
                    str(source.get("quote") or "")
                    for source in selected.sources
                    if str(source.get("quote") or "")
                ]
                matching_citation = {
                    "url": selected_urls[0],
                    "summary": selected_quotes[0] if selected_quotes else "",
                }
            else:
                mobile = None
        try:
            confidence = round(float(item.get("confidence", company.get("source_confidence", 70))))
        except (TypeError, ValueError):
            confidence = int(company.get("source_confidence") or 70)
        result = {
            "company_name": company_name,
            "mobile": mobile,
            "contact_person_name": item.get("contact_person_name"),
            "contact_person_role": item.get("contact_person_role"),
            "email": item.get("email"),
            "wechat": item.get("wechat"),
            "address": item.get("address"),
            "service_intro": item.get("service_intro"),
            "source_url": (
                matching_citation["url"]
                if matching_citation
                else (None if self.uses_responses_api else item.get("source_url"))
            ),
            "contact_evidence_url": matching_citation["url"] if matching_citation else None,
            "contact_evidence_urls": [matching_citation["url"]] if matching_citation else [],
            "contact_evidence_quote": (
                matching_citation["summary"]
                if matching_citation
                else (None if self.uses_responses_api else item.get("evidence_quote"))
            ),
            "contact_verification_status": (
                "verified_public_web" if matching_citation else "unverified_model_only"
            ),
            "matched_keyword": company.get("matched_keyword"),
            "source_confidence": max(0, min(100, confidence)),
        }
        if self.uses_responses_api and mobile and "selected" in locals():
            result.update(
                contact_evidence_urls=selected_urls,
                contact_evidence_quotes=selected_quotes,
                contact_verification_status="verified_cross_source",
                contact_evidence_grade=str(selected.grade),
                contact_evidence_score=selected.score,
            )
        elif matching_citation:
            result.update(contact_evidence_grade="A", contact_evidence_score=90)
        return result

    def _request_items(
        self,
        prompt: str,
        *,
        system_prompt: str,
        operation: str,
        use_web_search: bool = False,
    ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        response = self._request_json(
            prompt,
            system_prompt=system_prompt,
            operation=operation,
            use_web_search=use_web_search,
        )
        items = response.get("items")
        if not isinstance(items, list):
            raise ModelVerificationError(f"model {self.id} {operation} failed")
        citations = response.get("_web_citations") or []
        return items, [item for item in citations if isinstance(item, dict)]

    def parse_intent_keywords(self, request_text: str) -> list[str]:
        if not self.configured:
            raise ModelVerificationError(f"model {self.id} is not configured")
        prompt = (
            f"用户需求：{request_text}\n"
            "提取需要用于公开网页搜索的客户类型关键词。针对别墅、高端住宅、豪宅、私宅、软装、陈设、全案、室内等需求，"
            "补全住宅相关的同义搜索词并去重；不要扩展到工业、产品、平面、品牌、广告、景观、建材或招聘培训。"
            "不得返回公司名称、联系方式、城市或解释。严格返回 JSON："
            '{"keywords":["全案软装公司","设计公司","整装公司"]}'
        )
        keywords = self._request_json(
            prompt,
            system_prompt="你只负责生成企业搜索关键词。",
            operation="intent parsing",
        ).get("keywords")
        if not isinstance(keywords, list):
            raise ModelVerificationError(f"model {self.id} intent parsing failed")
        return list(dict.fromkeys(str(item).strip() for item in keywords if str(item).strip()))[:16]


def _confidence_level(score: int) -> str:
    if score >= 90:
        return "已核验"
    if score >= 70:
        return "较可信"
    return "低可信"


def apply_verification_consensus(
    leads: list[dict[str, Any]],
    verdicts_by_model: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    model_ids = list(verdicts_by_model)
    result: list[dict[str, Any]] = []
    for index, lead in enumerate(leads):
        details = {model_id: verdicts[index] for model_id, verdicts in verdicts_by_model.items()}
        decisions = [bool(detail["relevant"]) for detail in details.values()]
        if not decisions:
            status = lead.get("verification_status") or "source_only"
        elif len(decisions) == 1:
            status = "single_model_match" if decisions[0] else "single_model_reject"
        elif all(decisions):
            status = "model_consensus_match"
        elif not any(decisions):
            status = "model_consensus_reject"
        else:
            status = "model_disagreement"
        model_confidences = [
            max(0, min(100, int(detail.get("confidence", 85 if detail.get("relevant") else 30))))
            for detail in details.values()
        ]
        source_confidence = max(0, min(100, int(lead.get("source_confidence") or 0)))
        confidence_score = round(
            (source_confidence + sum(model_confidences)) / (1 + len(model_confidences))
        )
        result.append(
            {
                **lead,
                "verification_status": status,
                "verification_models": model_ids,
                "verification_detail": details,
                "confidence_score": confidence_score,
                "confidence_level": _confidence_level(confidence_score),
            }
        )
    return result


def build_model_verifiers(settings: Settings) -> dict[str, ModelVerifier]:
    relay_configured = bool(
        settings.doubao_relay_api_key
        and settings.doubao_relay_base_url
        and settings.doubao_relay_model
    )
    doubao_api_key = (
        settings.doubao_relay_api_key.get_secret_value()
        if relay_configured and settings.doubao_relay_api_key
        else settings.doubao_api_key.get_secret_value()
        if settings.doubao_api_key
        else None
    )
    doubao_base_url = (
        settings.doubao_relay_base_url if relay_configured else settings.doubao_base_url
    )
    doubao_model = settings.doubao_relay_model if relay_configured else settings.doubao_model
    return {
        "deepseek": OpenAICompatibleVerifier(
            id="deepseek",
            label="DeepSeek",
            api_key=settings.deepseek_api_key.get_secret_value() if settings.deepseek_api_key else None,
            base_url=settings.deepseek_base_url,
            model=settings.deepseek_model,
        ),
        "doubao": OpenAICompatibleVerifier(
            id="doubao",
            label="豆包",
            api_key=doubao_api_key,
            base_url=doubao_base_url,
            model=doubao_model,
            required_model_prefix=(
                "ep-"
                if not relay_configured and "volces.com" in (doubao_base_url or "").lower()
                else None
            ),
        ),
    }
