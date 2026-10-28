import httpx
import pytest

from app.collection.models import (
    ModelAccountError,
    OpenAICompatibleVerifier,
    apply_verification_consensus,
    build_model_verifiers,
)
from app.config import Settings


def _lead() -> dict:
    return {
        "company_name": "武汉真实软装有限公司",
        "mobile": "13800138000",
        "email": "sales@example.com",
        "wechat": None,
        "address": "武汉市江汉区",
        "service_intro": "全案软装服务",
        "source_url": "https://example.com/contact",
        "source_confidence": 70,
        "verification_status": "source_verified",
    }


def test_openai_compatible_verifier_parses_grounded_json_verdicts() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/chat/completions")
        payload = __import__("json").loads(request.content)
        prompt = payload["messages"][1]["content"]
        assert "sales@example.com" in prompt
        assert "不得新增" in prompt
        assert "confidence" in prompt
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '{"items":[{"index":0,"relevant":true,"confidence":88,"reason":"服务匹配"}]}'
                        }
                    }
                ]
            },
        )

    verifier = OpenAICompatibleVerifier(
        id="deepseek",
        label="DeepSeek",
        api_key="secret",
        base_url="https://api.example.test/v1",
        model="test-model",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert verifier.verify([_lead()], request_text="寻找全案软装公司", city="武汉市", keyword="软装公司") == [
        {"relevant": True, "confidence": 88, "reason": "服务匹配"}
    ]


def test_openai_compatible_verifier_batches_large_evidence_sets() -> None:
    batch_sizes: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = __import__("json").loads(request.content)
        prompt = payload["messages"][1]["content"]
        evidence = __import__("json").loads(prompt.split("证据：", 1)[1])
        batch_sizes.append(len(evidence))
        items = [
            {"index": index, "relevant": True, "confidence": 82, "reason": "符合"}
            for index in range(len(evidence))
        ]
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": __import__("json").dumps({"items": items})}}]},
        )

    verifier = OpenAICompatibleVerifier(
        id="deepseek",
        label="DeepSeek",
        api_key="secret",
        base_url="https://api.example.test/v1",
        model="test-model",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    verdicts = verifier.verify(
        [{**_lead(), "company_name": f"公司 {index}"} for index in range(17)],
        request_text="寻找软装公司",
        city="武汉市",
        keyword="软装公司",
    )

    assert batch_sizes == [8, 8, 1]
    assert len(verdicts) == 17


def test_consensus_distinguishes_single_agreement_and_disagreement() -> None:
    single = apply_verification_consensus([_lead()], {"deepseek": [{"relevant": True, "reason": "匹配"}]})
    agreed = apply_verification_consensus(
        [_lead()],
        {
            "deepseek": [{"relevant": True, "reason": "匹配"}],
            "doubao": [{"relevant": True, "reason": "符合"}],
        },
    )
    disagreed = apply_verification_consensus(
        [_lead()],
        {
            "deepseek": [{"relevant": True, "reason": "匹配"}],
            "doubao": [{"relevant": False, "reason": "不确定"}],
        },
    )

    assert single[0]["verification_status"] == "single_model_match"
    assert single[0]["verification_models"] == ["deepseek"]
    assert single[0]["confidence_score"] == 78
    assert single[0]["confidence_level"] == "较可信"
    assert agreed[0]["verification_status"] == "model_consensus_match"
    assert disagreed[0]["verification_status"] == "model_disagreement"
    assert disagreed[0]["confidence_score"] < 70


def test_source_and_model_confidence_can_reach_verified_level() -> None:
    lead = {**_lead(), "source_confidence": 95}

    result = apply_verification_consensus(
        [lead],
        {"deepseek": [{"relevant": True, "confidence": 85, "reason": "匹配"}]},
    )

    assert result[0]["confidence_score"] == 90
    assert result[0]["confidence_level"] == "已核验"


def test_rejected_model_verdict_lowers_confidence_below_display_threshold() -> None:
    result = apply_verification_consensus(
        [_lead()],
        {"deepseek": [{"relevant": False, "confidence": 30, "reason": "不匹配"}]},
    )

    assert result[0]["confidence_score"] == 50
    assert result[0]["confidence_level"] == "低可信"


def test_doubao_requires_an_ark_endpoint_id() -> None:
    unavailable = OpenAICompatibleVerifier(
        id="doubao",
        label="豆包",
        api_key="secret",
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        model="doubao-seed-2.1-turbo",
        required_model_prefix="ep-",
    )
    available = OpenAICompatibleVerifier(
        id="doubao",
        label="豆包",
        api_key="secret",
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        model="ep-valid-endpoint",
        required_model_prefix="ep-",
    )

    assert unavailable.configured is False
    assert available.configured is True


def test_doubao_compatible_proxy_accepts_direct_model_name() -> None:
    settings = Settings(
        app_env="test",
        database_url="sqlite+pysqlite:///:memory:",
        app_master_key="test-master-key",
        session_secret="test-session-secret",
        doubao_api_key="secret",
        doubao_base_url="https://api.kuai.host/v1",
        doubao_model="doubao-seed-2-0-lite-260428",
    )

    assert build_model_verifiers(settings)["doubao"].configured is True


def test_doubao_relay_configuration_takes_precedence_over_official_ark() -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        database_url="sqlite+pysqlite:///:memory:",
        app_master_key="test-master-key",
        session_secret="test-session-secret",
        doubao_api_key="official-secret",
        doubao_base_url="https://ark.cn-beijing.volces.com/api/v3",
        doubao_model="ep-official",
        doubao_relay_api_key="relay-secret",
        doubao_relay_base_url="https://relay.example.test/v1",
        doubao_relay_model="doubao-relay-model",
    )

    verifier = build_model_verifiers(settings)["doubao"]

    assert isinstance(verifier, OpenAICompatibleVerifier)
    assert verifier.api_key == "relay-secret"
    assert verifier.base_url == "https://relay.example.test/v1"
    assert verifier.model == "doubao-relay-model"
    assert verifier.required_model_prefix is None
    assert verifier.uses_responses_api is False


def test_doubao_relay_discovery_uses_chat_completions_without_web_search_tools() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        payload = __import__("json").loads(request.content)
        assert "messages" in payload
        assert "input" not in payload
        assert "tools" not in payload
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"items":[]}'}}]})

    verifier = OpenAICompatibleVerifier(
        id="doubao",
        label="豆包",
        api_key="relay-secret",
        base_url="https://relay.example.test/v1",
        model="doubao-relay-model",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert verifier.discover_companies(
        request_text="查找深圳软装公司",
        city="深圳市",
        keywords=["软装公司"],
        limit=10,
    ) == []


def test_official_ark_uses_responses_api_without_web_search_for_intent() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = __import__("json").loads(request.content)
        assert request.url.path == "/api/v3/responses"
        assert "input" in payload
        assert "messages" not in payload
        assert "tools" not in payload
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": '{"keywords":["软装公司"]}',
                            }
                        ],
                    }
                ]
            },
        )

    verifier = OpenAICompatibleVerifier(
        id="doubao",
        label="豆包",
        api_key="secret",
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        model="ep-test",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        required_model_prefix="ep-",
    )

    assert verifier.parse_intent_keywords("寻找软装公司") == ["软装公司"]


def _ark_verifier(handler) -> OpenAICompatibleVerifier:
    return OpenAICompatibleVerifier(
        id="doubao",
        label="豆包",
        api_key="secret",
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        model="ep-test",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        required_model_prefix="ep-",
    )


def _ark_items_response(
    items: list[dict], annotations: list[dict] | None = None
) -> httpx.Response:
    content = __import__("json").dumps({"items": items}, ensure_ascii=False)
    return httpx.Response(
        200,
        json={
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": content,
                            "annotations": annotations or [],
                        }
                    ],
                }
            ]
        },
    )


def _assert_web_search_payload(payload: dict) -> None:
    assert payload["tools"] == [{"type": "web_search", "max_keyword": 2, "limit": 10}]
    assert payload["max_tool_calls"] == 2


def test_official_ark_discovery_enables_web_search() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = __import__("json").loads(request.content)
        _assert_web_search_payload(payload)
        return _ark_items_response([])

    verifier = _ark_verifier(handler)

    assert verifier.discover(request_text="寻找软装公司", city="深圳市", keyword="软装公司") == []


def test_official_ark_company_discovery_enables_web_search() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = __import__("json").loads(request.content)
        _assert_web_search_payload(payload)
        return _ark_items_response([])

    verifier = _ark_verifier(handler)

    assert verifier.discover_companies(
        request_text="寻找软装公司",
        city="深圳市",
        keywords=["软装公司"],
    ) == []


def test_official_ark_surfaces_account_overdue_as_non_retryable_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "error": {
                    "code": "AccountOverdueError",
                    "message": "account has an overdue balance",
                }
            },
        )

    with pytest.raises(ModelAccountError, match="AccountOverdueError"):
        _ark_verifier(handler).discover_companies(
            request_text="查深圳软装公司",
            city="深圳市",
            keywords=["软装公司"],
        )


def test_official_ark_company_enrichment_enables_web_search() -> None:
    company_name = "深圳真实软装有限公司"

    def handler(request: httpx.Request) -> httpx.Response:
        payload = __import__("json").loads(request.content)
        _assert_web_search_payload(payload)
        return _ark_items_response(
            [
                {
                    "company_name": company_name,
                    "mobile": None,
                    "email": None,
                    "wechat": None,
                    "address": None,
                    "service_intro": None,
                    "source_url": None,
                    "evidence_quote": None,
                    "confidence": 70,
                }
            ]
        )

    verifier = _ark_verifier(handler)

    lead = verifier.enrich_company(
        request_text="寻找软装公司",
        city="深圳市",
        company={"company_name": company_name, "matched_keyword": "软装公司"},
        required_contacts=["mobile"],
    )

    assert lead["company_name"] == company_name


def test_official_ark_company_discovery_keeps_only_citation_grounded_legal_names() -> None:
    company_name = "深圳市真实软装设计有限公司"

    def handler(request: httpx.Request) -> httpx.Response:
        return _ark_items_response(
            [
                {
                    "company_name": company_name,
                    "matched_keyword": "软装公司",
                    "reason": "提供别墅软装服务",
                    "confidence": 82,
                },
                {
                    "company_name": "室内设计公司",
                    "matched_keyword": "设计公司",
                    "reason": "通用名称",
                    "confidence": 90,
                },
            ],
            annotations=[
                {
                    "type": "url_citation",
                    "url": "https://registry.example/company-1",
                    "title": company_name,
                    "summary": f"{company_name}，地址位于深圳市，主营别墅软装设计。",
                }
            ],
        )

    companies = _ark_verifier(handler).discover_companies(
        request_text="查深圳别墅软装公司",
        city="深圳市",
        keywords=["别墅软装公司"],
    )

    assert companies == [
        {
            "company_name": company_name,
            "matched_keyword": "软装公司",
            "reason": "提供别墅软装服务",
            "source_confidence": 82,
            "source_url": "https://registry.example/company-1",
            "company_name_evidence_urls": ["https://registry.example/company-1"],
            "company_name_verification_status": "verified_public_web",
        }
    ]


def test_official_ark_enrichment_uses_matching_citation_instead_of_model_url() -> None:
    company_name = "深圳市真实软装设计有限公司"
    mobile = "13800138000"
    quote = f"{company_name} 联系手机 {mobile}，主营别墅软装设计。"

    def handler(request: httpx.Request) -> httpx.Response:
        return _ark_items_response(
            [
                {
                    "company_name": company_name,
                    "mobile": mobile,
                    "source_url": "https://hallucinated.example/contact",
                    "confidence": 85,
                }
            ],
            annotations=[
                {
                    "type": "url_citation",
                    "url": "https://public.example/company-contact",
                    "title": company_name,
                    "summary": quote,
                }
            ],
        )

    lead = _ark_verifier(handler).enrich_company(
        request_text="查深圳别墅软装公司",
        city="深圳市",
        company={"company_name": company_name, "matched_keyword": "软装公司"},
        required_contacts=["mobile"],
    )

    assert lead["mobile"] == mobile
    assert lead["source_url"] == "https://public.example/company-contact"
    assert lead["contact_evidence_url"] == "https://public.example/company-contact"
    assert lead["contact_evidence_quote"] == quote
    assert lead["contact_verification_status"] == "verified_public_web"


def test_official_ark_enrichment_discards_mobile_without_matching_citation() -> None:
    company_name = "深圳市真实软装设计有限公司"

    def handler(request: httpx.Request) -> httpx.Response:
        return _ark_items_response(
            [{"company_name": company_name, "mobile": "13800138000", "confidence": 85}],
            annotations=[
                {
                    "type": "url_citation",
                    "url": "https://public.example/other-company",
                    "title": "其他公司",
                    "summary": "其他公司联系电话 13900139000",
                }
            ],
        )

    lead = _ark_verifier(handler).enrich_company(
        request_text="查深圳别墅软装公司",
        city="深圳市",
        company={"company_name": company_name, "matched_keyword": "软装公司"},
        required_contacts=["mobile"],
    )

    assert lead["mobile"] is None
    assert lead["contact_verification_status"] == "unverified_model_only"
    assert lead["contact_evidence_url"] is None


def test_official_ark_enrichment_uses_alias_then_reverse_mobile_search() -> None:
    company_name = "深圳市乙空间设计有限公司"
    mobile = "13900139000"
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = __import__("json").loads(request.content)
        prompt = payload["input"][1]["content"][0]["text"]
        calls.append(prompt)
        if "反向核验手机号" in prompt:
            return _ark_items_response(
                [{"company_name": company_name, "mobile": mobile, "confidence": 88}],
                annotations=[
                    {
                        "type": "url_citation",
                        "url": "https://project-news.example/yi-villa",
                        "title": company_name,
                        "summary": f"{mobile} 为{company_name}业务咨询手机号。",
                    }
                ],
            )
        if "品牌或简称" in prompt:
            return _ark_items_response(
                [{"company_name": company_name, "mobile": mobile, "confidence": 82}],
                annotations=[
                    {
                        "type": "url_citation",
                        "url": "https://design-directory.example/yi",
                        "title": "乙空间别墅设计",
                        "summary": f"乙空间别墅全案设计联系电话 {mobile}。",
                    }
                ],
            )
        return _ark_items_response(
            [{"company_name": company_name, "mobile": None, "confidence": 70}]
        )

    lead = _ark_verifier(handler).enrich_company(
        request_text="查深圳别墅软装公司",
        city="深圳市",
        company={
            "company_name": company_name,
            "matched_keyword": "别墅全案设计",
        },
        required_contacts=["mobile"],
    )

    assert len(calls) == 3
    assert lead["mobile"] == mobile
    assert lead["contact_verification_status"] == "verified_cross_source"
    assert lead["contact_evidence_grade"] == "B"
    assert lead["contact_evidence_score"] >= 80
    assert lead["contact_evidence_urls"] == [
        "https://design-directory.example/yi",
        "https://project-news.example/yi-villa",
    ]


def test_official_ark_evidence_verification_does_not_enable_web_search() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = __import__("json").loads(request.content)
        assert "tools" not in payload
        return _ark_items_response(
            [{"index": 0, "relevant": True, "confidence": 88, "reason": "符合"}]
        )

    verifier = _ark_verifier(handler)

    assert verifier.verify(
        [_lead()],
        request_text="寻找软装公司",
        city="深圳市",
        keyword="软装公司",
    )[0]["relevant"] is True


def test_model_can_discover_grounded_company_candidates() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = __import__("json").loads(request.content)
        prompt = payload["messages"][1]["content"]
        assert "武汉市" in prompt
        assert "软装公司" in prompt
        assert "约 50%" in prompt
        assert "最多返回 50 家" in prompt
        assert "联系方式不确定可填 null" in prompt
        assert "至少一种手机/邮箱/微信" not in prompt
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": """
                {"items":[{"company_name":"武汉真实软装有限公司","mobile":"13800138000",
                "email":null,"wechat":null,"address":"武汉市江汉区","service_intro":"全案软装",
                "source_url":null,"confidence":76}]}
                """}}]
            },
        )

    verifier = OpenAICompatibleVerifier(
        id="deepseek",
        label="DeepSeek",
        api_key="secret",
        base_url="https://api.example.test/v1",
        model="test-model",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    leads = verifier.discover(request_text="寻找全案软装公司", city="武汉市", keyword="软装公司")

    assert leads == [{
        "company_name": "武汉真实软装有限公司",
        "mobile": "13800138000",
        "email": None,
        "wechat": None,
        "address": "武汉市江汉区",
        "service_intro": "全案软装",
        "source_url": None,
        "matched_keyword": "软装公司",
        "source_confidence": 76,
    }]


def test_model_discovers_company_names_without_requesting_contacts() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = __import__("json").loads(request.content)
        prompt = payload["messages"][1]["content"]
        assert "武汉市" in prompt
        assert "软装公司" in prompt
        assert "整装公司" in prompt
        assert "最多返回 100 家" in prompt
        assert "第一阶段只返回候选公司" in prompt
        assert "mobile" not in prompt
        assert "email" not in prompt
        assert "wechat" not in prompt
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": """
            {"items":[{"company_name":"武汉真实装饰有限公司","matched_keyword":"整装公司",
            "reason":"提供整装服务","confidence":72}]}
            """}}]},
        )

    verifier = OpenAICompatibleVerifier(
        id="deepseek",
        label="DeepSeek",
        api_key="secret",
        base_url="https://api.example.test/v1",
        model="test-model",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    companies = verifier.discover_companies(
        request_text="找武汉软装和整装公司",
        city="武汉市",
        keywords=["软装公司", "整装公司"],
    )

    assert companies == [{
        "company_name": "武汉真实装饰有限公司",
        "matched_keyword": "整装公司",
        "reason": "提供整装服务",
        "source_confidence": 72,
    }]


def test_model_enriches_exactly_one_company() -> None:
    company_name = "武汉真实装饰有限公司"

    def handler(request: httpx.Request) -> httpx.Response:
        payload = __import__("json").loads(request.content)
        prompt = payload["messages"][1]["content"]
        assert prompt.count(company_name) == 1
        assert "一次只能查询这一家公司" in prompt
        assert "请联网搜索" in prompt
        assert "直接给出" in prompt
        assert "搜索不到" in prompt
        assert "来源 URL" in prompt
        assert "证据原文" in prompt
        assert "不得推测" in prompt
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": """
            {"items":[{"company_name":"武汉真实装饰有限公司","mobile":"13800138000",
            "contact_person_name":"张三","contact_person_role":"业务负责人",
            "email":null,"wechat":null,"address":"武汉市江汉区","service_intro":"全案整装",
            "source_url":"https://example.test/company",
            "evidence_quote":"武汉真实装饰有限公司 业务负责人张三 13800138000","confidence":78}]}
            """}}]},
        )

    verifier = OpenAICompatibleVerifier(
        id="deepseek",
        label="DeepSeek",
        api_key="secret",
        base_url="https://api.example.test/v1",
        model="test-model",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    lead = verifier.enrich_company(
        request_text="找武汉整装公司",
        city="武汉市",
        company={"company_name": company_name, "matched_keyword": "整装公司"},
        required_contacts=["mobile"],
    )

    assert lead["company_name"] == company_name
    assert lead["mobile"] == "13800138000"
    assert lead["source_url"] == "https://example.test/company"
    assert lead["contact_person_name"] == "张三"
    assert lead["contact_person_role"] == "业务负责人"
    assert lead["contact_evidence_quote"] == (
        "武汉真实装饰有限公司 业务负责人张三 13800138000"
    )


def test_model_parses_only_normalized_search_keywords() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = __import__("json").loads(request.content)
        assert "不得返回公司" in payload["messages"][1]["content"]
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"keywords":["全案软装公司","设计公司","整装公司"]}'}}]},
        )

    verifier = OpenAICompatibleVerifier(
        id="doubao",
        label="豆包",
        api_key="secret",
        base_url="https://api.example.test/v1",
        model="ep-test",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        required_model_prefix="ep-",
    )

    assert verifier.parse_intent_keywords("寻找全案软装、设计和整装公司") == [
        "全案软装公司",
        "设计公司",
        "整装公司",
    ]
