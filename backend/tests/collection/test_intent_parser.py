from app.collection.intent import parse_collection_intent


def test_defaults_to_all_cities_doubao_and_mobile() -> None:
    intent = parse_collection_intent("寻找全案软装公司", configured_models={"doubao"})

    assert intent.all_cities is True
    assert intent.cities == []
    assert intent.models == ["doubao"]
    assert intent.required_contacts == ["mobile"]
    assert intent.contact_match == "all"
    assert intent.keywords == ["全案软装公司"]


def test_explicit_contacts_default_to_any_match() -> None:
    intent = parse_collection_intent("找武汉软装公司，要手机、邮箱或微信", configured_models={"doubao"})

    assert intent.cities == ["武汉市"]
    assert intent.required_contacts == ["mobile", "email", "wechat"]
    assert intent.contact_match == "any"


def test_explicit_all_contacts_uses_all_match() -> None:
    intent = parse_collection_intent("找长沙整装公司，手机和邮箱都要", configured_models={"doubao"})

    assert intent.cities == ["长沙市"]
    assert intent.required_contacts == ["mobile", "email"]
    assert intent.contact_match == "all"


def test_model_and_limit_are_extracted() -> None:
    intent = parse_collection_intent("用 DeepSeek 查成都设计公司，只要 10 家", configured_models={"deepseek"})

    assert intent.cities == ["成都市"]
    assert intent.models == ["deepseek"]
    assert intent.keywords == ["设计公司"]
    assert intent.limit_per_city == 10


def test_effective_lead_target_is_extracted_from_natural_language() -> None:
    intent = parse_collection_intent(
        "查深圳专做别墅项目的软装公司，最终至少得到30条有效客户信息，每条要真实手机号",
        configured_models={"doubao"},
    )

    assert intent.limit_per_city == 30


def test_residential_request_expands_bounded_related_keywords_and_drops_generic_design() -> None:
    intent = parse_collection_intent(
        "查深圳专做别墅项目的软装公司、全案设计公司和室内设计公司",
        configured_models={"doubao"},
    )

    assert "设计公司" not in intent.keywords
    assert "别墅软装设计公司" in intent.keywords
    assert "别墅全案设计公司" in intent.keywords
    assert "高端住宅设计公司" in intent.keywords
    assert "住宅软装设计公司" in intent.keywords
    assert len(intent.keywords) <= 24


def test_explicit_specific_keyword_is_preserved_during_residential_expansion() -> None:
    intent = parse_collection_intent(
        "查深圳别墅软装公司",
        configured_models={"doubao"},
        model_keywords=["私宅设计公司", "工业设计公司"],
    )

    assert intent.keywords[0] == "软装公司"
    assert "私宅设计公司" in intent.keywords
    assert "工业设计公司" not in intent.keywords


def test_doubao_falls_back_to_deepseek_when_only_deepseek_is_configured() -> None:
    intent = parse_collection_intent("寻找整装公司", configured_models={"deepseek"})

    assert intent.models == ["deepseek"]
