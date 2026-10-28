import pytest

from app.collection.doubao_browser import PlaywrightDoubaoAdapter
from app.collection.doubao_web import (
    DoubaoWebResponseError,
    build_contact_prompt,
    build_discovery_prompt,
    parse_contact_response,
    parse_discovery_response,
)


class _Editor:
    def fill(self, _prompt: str) -> None:
        pass

    def press(self, _key: str) -> None:
        pass


class _SequencedAdapter(PlaywrightDoubaoAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.responses = iter(["旧回答"] * 4 + ["新回答"] * 9)

    def _find_editor(self) -> _Editor:
        return _Editor()

    def _latest_response_text(self) -> str:
        return next(self.responses)

    def _is_generating(self) -> bool:
        return False


def test_parse_discovery_pipe_rows_keeps_source_and_quote() -> None:
    text = "1｜深圳市测试设计有限公司｜https://example.cn/about｜公司原文包含别墅全案"

    result = parse_discovery_response(text, city="深圳市")

    assert result[0].company_name == "深圳市测试设计有限公司"
    assert result[0].source_urls == ["https://example.cn/about"]
    assert result[0].evidence_quotes == ["公司原文包含别墅全案"]


def test_parse_discovery_markdown_table_keeps_company_columns() -> None:
    text = """| 序号 | 公司全称 | 可公开访问的来源URL | 证明业务匹配的页面原文 |
| --- | --- | --- | --- |
| 1 | 深圳市测试设计有限公司 | https://example.cn/about | 主营别墅全案设计 |"""

    result = parse_discovery_response(text, city="深圳市")

    assert result[0].company_name == "深圳市测试设计有限公司"
    assert result[0].source_urls == ["https://example.cn/about"]
    assert result[0].evidence_quotes == ["主营别墅全案设计"]


def test_parse_discovery_filters_unrelated_design_and_supplier_rows() -> None:
    text = '''[
      {"company_name":"深圳市真实别墅软装有限公司","matched_keyword":"别墅软装设计公司",
       "source_urls":["https://example.cn/real"],"evidence_quotes":["主营别墅软装设计"]},
      {"company_name":"深圳市乐图工业设计有限公司","matched_keyword":"设计公司",
       "source_urls":["https://example.cn/industrial"],"evidence_quotes":["主营工业产品设计"]},
      {"company_name":"深圳市某建材有限公司","matched_keyword":"设计公司",
       "source_urls":["https://example.cn/material"],"evidence_quotes":["招聘软装设计师，销售建材"]}
    ]'''

    result = parse_discovery_response(
        text,
        city="深圳市",
        keywords=["别墅软装设计公司"],
    )

    assert [item.company_name for item in result] == ["深圳市真实别墅软装有限公司"]


def test_browser_adapter_waits_for_response_to_change(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.collection.doubao_browser.sleep", lambda _seconds: None)

    result = _SequencedAdapter().submit_prompt("新问题", timeout_seconds=2)

    assert result == "新回答"


def test_parse_discovery_fenced_json_deduplicates_urls() -> None:
    text = '''```json
    [{"company_name":"深圳市测试设计有限公司","matched_keyword":"别墅设计",
      "source_urls":["https://example.cn/about","https://example.cn/about"],
      "evidence_quotes":["主营别墅全案"]}]
    ```'''

    result = parse_discovery_response(text, city="深圳市")

    assert result[0].matched_keyword == "别墅设计"
    assert result[0].source_urls == ["https://example.cn/about"]


def test_contact_prompt_returns_seen_mobile_for_later_verification() -> None:
    prompt = build_contact_prompt(
        company_name="深圳市测试设计有限公司",
        city="深圳市",
        requested_contacts=["mobile"],
    )

    assert "先返回联网结果中实际出现的手机号" in prompt
    assert "系统会再次核验" in prompt
    assert "不得根据手机号、公司名称或常识推测" in prompt
    assert "自然语言" in prompt


def test_discovery_prompt_does_not_search_geo_directly() -> None:
    prompt = build_discovery_prompt(
        city="深圳市",
        keywords=["别墅软装公司", "全案设计公司"],
        request_text="查找深圳住宅软装和全案设计公司",
    )

    assert "优先查找公开提到GEO" not in prompt
    assert "不要以 GEO、SEO 或投放为检索词" in prompt
    assert "查不到这类记录时按原有公司搜索逻辑继续" in prompt


def test_parse_contact_normalizes_unicode_hyphenated_mobile() -> None:
    text = (
        "深圳市测试设计有限公司｜138‑0012‑3456｜"
        "https://example.cn/contact｜手机：13800123456"
    )

    result = parse_contact_response(text, company_name="深圳市测试设计有限公司")

    assert result.mobile == "13800123456"
    assert result.source_urls == ["https://example.cn/contact"]


def test_parse_contact_extracts_mobile_from_evidence_quote_when_field_is_empty() -> None:
    text = (
        '{"company_name":"深圳市测试设计有限公司","mobile":null,'
        '"source_urls":["https://example.cn/contact"],'
        '"evidence_quotes":["深圳市测试设计有限公司公开联系电话 13800123456"]}'
    )

    result = parse_contact_response(
        text,
        company_name="深圳市测试设计有限公司",
    )

    assert result.mobile == "13800123456"


def test_parse_contact_keeps_model_mobile_when_evidence_is_missing() -> None:
    text = (
        '{"company_name":"深圳市测试设计有限公司",'
        '"mobile":"13800123456","source_urls":[],"evidence_quotes":[]}'
    )

    result = parse_contact_response(
        text,
        company_name="深圳市测试设计有限公司",
    )

    assert result.mobile == "13800123456"
    assert result.source_urls == []


def test_parse_contact_extracts_mobile_from_explicit_natural_language_response() -> None:
    text = "我查询到深圳市测试设计有限公司公开联系电话为 138 0012 3456，暂未找到联系人姓名。"

    result = parse_contact_response(text, company_name="深圳市测试设计有限公司")

    assert result.mobile == "13800123456"
    assert result.company_name == "深圳市测试设计有限公司"


def test_parse_contact_preserves_all_explicit_natural_language_fields() -> None:
    text = (
        "深圳市测试设计有限公司公开信息：手机号 13800123456；"
        "联系人：罗震；职务：设计总监；邮箱：luozhen@example.cn；"
        "微信：luozhen_design；公司地址：深圳市南山区；"
        "服务简介：专注别墅软装与全案设计。"
    )

    result = parse_contact_response(text, company_name="深圳市测试设计有限公司")

    assert result.mobile == "13800123456"
    assert result.contact_person_name == "罗震"
    assert result.contact_person_role == "设计总监"
    assert result.email == "luozhen@example.cn"
    assert result.wechat == "luozhen_design"
    assert result.address == "深圳市南山区"
    assert result.service_intro == "专注别墅软装与全案设计"


def test_parse_contact_rejects_natural_language_mobile_without_target_company() -> None:
    text = "公开联系电话为 13800123456，但页面没有显示目标公司的名称。"

    with pytest.raises(DoubaoWebResponseError):
        parse_contact_response(text, company_name="深圳市测试设计有限公司")


def test_parse_contact_rejects_landline_and_model_only_mobile() -> None:
    text = "深圳市测试设计有限公司｜0755-12345678｜未提供来源｜联系电话"

    result = parse_contact_response(text, company_name="深圳市测试设计有限公司")

    assert result.mobile is None
    assert result.source_urls == []


def test_parse_contact_rejects_mismatched_company() -> None:
    text = "深圳市其他公司｜13800123456｜https://example.cn/contact｜公开手机"

    with pytest.raises(DoubaoWebResponseError):
        parse_contact_response(text, company_name="深圳市测试设计有限公司")


def test_parse_contact_keeps_phone_for_explicit_company_alias() -> None:
    text = (
        '{"company_name":"森肯设计","mobile":"13800123456",'
        '"source_urls":["https://example.cn/contact"],'
        '"evidence_quotes":["公开联系电话 13800123456"]}'
    )

    result = parse_contact_response(
        text,
        company_name="ACEDESIGN深圳森肯设计顾问有限公司",
    )

    assert result.company_name == "ACEDESIGN深圳森肯设计顾问有限公司"
    assert result.mobile == "13800123456"
    assert result.source_urls == ["https://example.cn/contact"]


def test_parse_contact_accepts_full_width_punctuation_in_company_name() -> None:
    text = (
        '{"company_name":"和慕装饰（深圳）有限公司","mobile":"13800123456",'
        '"source_urls":["https://example.cn/contact"],'
        '"evidence_quotes":["和慕装饰（深圳）有限公司 联系电话 13800123456"]}'
    )

    result = parse_contact_response(
        text,
        company_name="和慕装饰(深圳)有限公司",
    )

    assert result.company_name == "和慕装饰(深圳)有限公司"
    assert result.mobile == "13800123456"


def test_parse_contact_not_found_returns_empty_candidate() -> None:
    result = parse_contact_response(
        "未找到可核验公开手机号",
        company_name="深圳市测试设计有限公司",
    )

    assert result.company_name == "深圳市测试设计有限公司"
    assert result.mobile is None


def test_malformed_discovery_output_raises() -> None:
    with pytest.raises(DoubaoWebResponseError):
        parse_discovery_response("这是一段无结构回答", city="深圳市")


def test_prompts_limit_discovery_and_require_one_contact_company() -> None:
    discovery = build_discovery_prompt(
        city="深圳市",
        keywords=["软装公司", "全案设计公司"],
        request_text="查深圳别墅项目服务商",
    )
    contact = build_contact_prompt(
        company_name="深圳市测试设计有限公司",
        city="深圳市",
        requested_contacts=["mobile"],
    )

    assert "最多5家" in discovery
    assert "不得编造" in discovery
    assert "工业设计" in discovery
    assert "不要输出与住宅、别墅或软装无关" in discovery
    assert "GEO" in discovery
    assert "不得把投放GEO当作事实" in discovery
    assert "只输出JSON" in discovery
    assert "只查询这1家公司" in contact
    assert "未公开" in contact
    assert "搜索结果摘要" in contact
    assert "深圳市深圳市测试设计有限公司" not in contact


def test_contact_prompt_requires_evidence_for_person_and_role() -> None:
    contact = build_contact_prompt(
        company_name="深圳市测试设计有限公司",
        city="深圳市",
        requested_contacts=["mobile"],
    )

    assert "contact_person_name" in contact
    assert "contact_person_role" in contact
    assert "姓氏加先生或女士" in contact
    assert "原文没有就填null" in contact
    assert "只输出JSON" in contact
