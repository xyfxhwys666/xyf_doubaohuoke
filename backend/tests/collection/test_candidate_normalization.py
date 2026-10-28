from app.collection.normalization import (
    deduplicate_company_candidates,
    deduplicate_leads,
    is_complete_company_entity_name,
    normalize_company_name,
    normalize_emails,
    normalize_lead,
)


def test_company_name_filter_rejects_search_result_titles() -> None:
    assert normalize_company_name("武汉某某装饰有限公司") == "武汉某某装饰有限公司"
    assert normalize_company_name("武汉软装公司排行榜") is None
    assert normalize_company_name("武汉装饰公司招聘信息") is None
    assert normalize_company_name("武汉整装公司联系方式") is None
    assert is_complete_company_entity_name("室内设计公司") is False
    assert is_complete_company_entity_name("价格软装深圳软装公司") is False
    assert is_complete_company_entity_name("深圳木作设计事务所") is True


def test_company_candidates_are_deduplicated_by_city_and_name() -> None:
    candidates = deduplicate_company_candidates([
        {
            "city": "武汉市",
            "company_name": "武汉某某装饰有限公司",
            "source_confidence": 65,
            "matched_keyword": "软装公司",
        },
        {
            "city": "武汉市",
            "company_name": " 武汉某某装饰有限公司 ",
            "source_confidence": 82,
            "matched_keyword": "整装公司",
        },
        {
            "city": "长沙市",
            "company_name": "武汉某某装饰有限公司",
            "source_confidence": 70,
            "matched_keyword": "软装公司",
        },
    ])

    assert len(candidates) == 2
    assert candidates[0]["source_confidence"] == 82
    assert candidates[0]["matched_keyword"] == "整装公司"


def test_company_candidate_dedup_preserves_equal_confidence_contact_fields() -> None:
    candidates = deduplicate_company_candidates(
        [
            {
                "city": "Shenzhen",
                "company_name": "Shenzhen Villa Design Co",
                "source_confidence": 70,
            },
            {
                "city": "Shenzhen",
                "company_name": " Shenzhen Villa Design Co ",
                "source_confidence": 70,
                "mobile": "13800138000",
                "email": "sales@example.test",
                "source_url": "https://example.test/contact",
            },
        ]
    )

    assert len(candidates) == 1
    assert candidates[0]["mobile"] == "13800138000"
    assert candidates[0]["email"] == "sales@example.test"
    assert candidates[0]["source_url"] == "https://example.test/contact"


def test_normalized_lead_keeps_contact_candidate_and_defaults_evidence_status() -> None:
    lead = normalize_lead(
        {
            "company_name": "深圳市甲软装设计有限公司",
            "mobile": "13800138000",
            "contact_person_name": "张三",
            "contact_person_role": "业务负责人",
            "source_url": "https://company.example/contact",
            "contact_evidence_quote": "公司全称 张三 13800138000",
        },
        city="深圳市",
        provider="doubao",
    )

    assert lead["contact_person_name"] == "张三"
    assert lead["contact_person_role"] == "业务负责人"
    assert lead["contact_evidence_url"] == "https://company.example/contact"
    assert lead["contact_evidence_quote"] == "公司全称 张三 13800138000"
    assert lead["contact_verification_status"] == "unverified"
    assert lead["contact_verified_at"] is None
    assert lead["contact_verification_reason"] == ""


def test_contact_evidence_bundle_is_merged_from_one_richest_duplicate() -> None:
    first = normalize_lead(
        {
            "company_name": "深圳市甲软装设计有限公司",
            "mobile": "13800138000",
        },
        city="深圳市",
        provider="doubao",
    )
    verified = {
        **normalize_lead(
            {
                "company_name": "深圳市甲软装设计有限公司",
                "mobile": "13800138000",
                "email": "sales@company.example",
                "contact_person_name": "张三",
                "contact_person_role": "业务负责人",
                "source_url": "https://company.example/contact",
                "contact_evidence_quote": "深圳市甲软装设计有限公司 张三 13800138000",
            },
            city="深圳市",
            provider="doubao",
        ),
        "contact_verification_status": "verified_public_web",
        "contact_verified_at": "2026-07-28T12:00:00+08:00",
        "contact_verification_reason": "同页证据通过",
    }

    merged = deduplicate_leads([first, verified])[0]

    assert merged["contact_person_name"] == "张三"
    assert merged["contact_person_role"] == "业务负责人"
    assert merged["contact_verification_status"] == "verified_public_web"
    assert merged["contact_evidence_url"] == "https://company.example/contact"


def test_normalize_lead_filters_non_business_service_intro_fragments() -> None:
    normalized = normalize_lead(
        {
            "company_name": "深圳市示例软装有限公司",
            "service_intro": (
                "更新时间：2026-07-30。驾车导航，地址：深圳市南山区示例路。"
                "专注别墅软装与全案设计，提供空间陈设和家具定制。联系电话：13800138000。"
            ),
        },
        city="深圳市",
        provider="doubao",
        keyword="别墅软装",
    )

    intro = normalized["service_intro"]
    assert "别墅软装" in intro
    assert "全案设计" in intro
    assert "更新时间" not in intro
    assert "驾车导航" not in intro
    assert "13800138000" not in intro


def test_normalize_emails_keeps_at_most_two_most_credible_addresses() -> None:
    emails = normalize_emails(
        "sales@example.com; info@acme-design.cn; owner@gmail.com; contact@acme-design.cn",
        company_name="深圳市 Acme Design 设计有限公司",
        source_urls=["https://www.acme-design.cn/contact"],
        evidence_quotes=[
            "深圳市 Acme Design 设计有限公司公开邮箱：sales@example.com；"
            "info@acme-design.cn；owner@gmail.com；contact@acme-design.cn"
        ],
    )

    assert emails == ["info@acme-design.cn", "contact@acme-design.cn"]


def test_normalize_lead_serializes_only_two_selected_emails() -> None:
    lead = normalize_lead(
        {
            "company_name": "深圳市 Acme Design 设计有限公司",
            "email": "['info@acme-design.cn', 'contact@acme-design.cn', 'owner@gmail.com']",
            "source_url": "https://www.acme-design.cn/contact",
            "contact_evidence_quotes": [
                "深圳市 Acme Design 设计有限公司 info@acme-design.cn contact@acme-design.cn"
            ],
        },
        city="深圳市",
        provider="doubao",
    )

    assert lead["email"] == "info@acme-design.cn; contact@acme-design.cn"
