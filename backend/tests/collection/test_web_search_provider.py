from threading import Lock
from time import sleep

import httpx
import pytest

from app.collection.providers import ProviderError
from app.collection.web_search import (
    CompanyNameEvidence,
    ContactEvidence,
    WebSearchProvider,
    _SearchResultParser,
    _visible_text,
    clean_company_name,
    clean_service_intro,
)


def test_public_search_client_uses_bounded_request_timeout() -> None:
    provider = WebSearchProvider()
    try:
        assert provider.client.timeout.read == 8
        assert provider.client.timeout.connect == 8
    finally:
        provider.client.close()


def test_contact_verification_stops_before_request_when_company_deadline_is_expired() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, text="should not be fetched", request=request)

    provider = WebSearchProvider(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        request_interval_seconds=0,
        contact_verification_timeout_seconds=0,
    )

    evidence = provider.verify_contact_evidence(
        company_name="深圳市甲软装设计有限公司",
        mobile="13800138000",
        source_url="https://company.example/contact",
        contact_person_name=None,
        contact_person_role=None,
    )

    assert evidence.status == "verification_timeout"
    assert calls == 0


def test_contact_verification_limits_direct_source_pages() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        text = "深圳市甲软装设计有限公司业务咨询 13800138000" if "source-2" in str(request.url) else "无关页面"
        return httpx.Response(200, text=text, request=request)

    provider = WebSearchProvider(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        request_interval_seconds=0,
        max_contact_source_urls=2,
    )

    evidence = provider.verify_contact_evidence(
        company_name="深圳市甲软装设计有限公司",
        mobile="13800138000",
        source_url="https://source-1.example/contact",
        source_urls=[
            "https://source-2.example/contact",
            "https://source-3.example/contact",
            "https://source-4.example/contact",
        ],
        contact_person_name=None,
        contact_person_role=None,
    )

    assert evidence.status == "verified_official_page"
    assert requested == [
        "https://source-1.example/contact",
        "https://source-2.example/contact",
    ]


def test_official_page_requires_exact_company_and_mobile_same_page() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=(
                "<p>深圳市甲软装设计有限公司</p>"
                "<p>业务负责人：张三 13800138000</p>"
            ),
            request=request,
        )

    provider = WebSearchProvider(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        request_interval_seconds=0,
    )

    evidence = provider.verify_contact_evidence(
        company_name="深圳市甲软装设计有限公司",
        mobile="13800138000",
        source_url="https://company.example/contact",
        contact_person_name="张三",
        contact_person_role="业务负责人",
    )

    assert evidence.status == "verified_official_page"
    assert evidence.evidence_url == "https://company.example/contact"
    assert evidence.evidence_urls == ["https://company.example/contact"]
    assert evidence.contact_person_name == "张三"
    assert evidence.contact_person_role == "业务负责人"
    assert "13800138000" in evidence.evidence_quote


def test_official_page_requires_exact_company_and_email_same_page() -> None:
    email = "sales@company.example"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=(
                "<p>深圳市甲软装设计有限公司</p>"
                f"<p>业务邮箱：{email}</p>"
            ),
            request=request,
        )

    provider = WebSearchProvider(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        request_interval_seconds=0,
    )

    evidence = provider.verify_contact_evidence(
        company_name="深圳市甲软装设计有限公司",
        mobile=None,
        email=email,
        source_url="https://company.example/contact",
        contact_person_name=None,
        contact_person_role=None,
    )

    assert evidence.status == "verified_official_page"
    assert evidence.evidence_url == "https://company.example/contact"
    assert email in evidence.evidence_quote


def test_email_contact_verification_searches_exact_email_after_source_login() -> None:
    company_name = "深圳市邮箱回查设计有限公司"
    email = "sales@email-recheck.example"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "login.example":
            return httpx.Response(200, text="请登录后继续访问", request=request)
        if request.url.host == "cn.bing.com":
            query = request.url.params.get("q") or ""
            if email not in query:
                return httpx.Response(200, text="<html>没有结果</html>", request=request)
            return httpx.Response(
                200,
                text=(
                    f'<li class="b_algo"><h2><a href="https://company.example/contact">'
                    f"{company_name} 联系方式</a></h2><p>{company_name} 邮箱 {email}</p></li>"
                ),
                request=request,
            )
        if request.url.host in {"www.sogou.com", "www.so.com"}:
            return httpx.Response(200, text="<html>没有结果</html>", request=request)
        return httpx.Response(404, request=request)

    provider = WebSearchProvider(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        request_interval_seconds=0,
        retry_limit=0,
    )

    evidence = provider.verify_contact_evidence(
        company_name=company_name,
        mobile=None,
        email=email,
        source_url="https://login.example/company",
        contact_person_name=None,
        contact_person_role=None,
    )

    assert evidence.status == "verified_official_page"
    assert evidence.evidence_url == "https://company.example/contact"
    assert email in evidence.evidence_quote


def test_contact_evidence_drops_person_fields_missing_from_page() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="深圳市甲软装设计有限公司业务咨询 13800138000",
            request=request,
        )

    provider = WebSearchProvider(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        request_interval_seconds=0,
    )

    evidence = provider.verify_contact_evidence(
        company_name="深圳市甲软装设计有限公司",
        mobile="13800138000",
        source_url="https://company.example/contact",
        contact_person_name="张三",
        contact_person_role="创始人",
    )

    assert evidence.status == "verified_official_page"
    assert evidence.contact_person_name is None
    assert evidence.contact_person_role is None


def test_contact_evidence_drops_person_fields_not_near_verified_mobile() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=(
                "张三 创始人 "
                + ("无关页面内容 " * 80)
                + "深圳市甲软装设计有限公司业务咨询 13800138000"
            ),
            request=request,
        )

    provider = WebSearchProvider(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        request_interval_seconds=0,
    )

    evidence = provider.verify_contact_evidence(
        company_name="深圳市甲软装设计有限公司",
        mobile="13800138000",
        source_url="https://company.example/contact",
        contact_person_name="张三",
        contact_person_role="创始人",
    )

    assert evidence.status == "verified_official_page"
    assert evidence.contact_person_name is None
    assert evidence.contact_person_role is None


@pytest.mark.parametrize(
    ("page_text", "expected_reason"),
    [
        ("深圳市乙软装设计有限公司 13800138000", "公司全称"),
        ("深圳市甲软装设计有限公司 13900139000", "手机号"),
    ],
)
def test_contact_evidence_rejects_page_without_same_company_and_mobile(
    page_text: str,
    expected_reason: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=page_text, request=request)

    provider = WebSearchProvider(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        request_interval_seconds=0,
    )

    evidence = provider.verify_contact_evidence(
        company_name="深圳市甲软装设计有限公司",
        mobile="13800138000",
        source_url="https://company.example/contact",
        contact_person_name=None,
        contact_person_role=None,
    )

    assert evidence.status == "rejected_mismatch"
    assert expected_reason in evidence.reason


def test_contact_evidence_rejects_invalid_mobile_without_requesting_page() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected request: {request.url}")

    provider = WebSearchProvider(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        request_interval_seconds=0,
    )

    evidence = provider.verify_contact_evidence(
        company_name="深圳市甲软装设计有限公司",
        mobile="0755-12345678",
        source_url="https://company.example/contact",
        contact_person_name=None,
        contact_person_role=None,
    )

    assert evidence.status == "rejected_mismatch"
    assert "11 位手机号" in evidence.reason


def test_contact_evidence_records_source_request_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, request=request)

    provider = WebSearchProvider(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        request_interval_seconds=0,
        retry_limit=0,
    )

    evidence = provider.verify_contact_evidence(
        company_name="深圳市甲软装设计有限公司",
        mobile="13800138000",
        source_url="https://company.example/contact",
        contact_person_name=None,
        contact_person_role=None,
    )

    assert evidence == ContactEvidence(
        status="verification_failed",
        evidence_url="https://company.example/contact",
        evidence_quote="",
        reason="联系方式来源页面请求失败",
    )


def test_contact_evidence_accepts_exact_pair_from_indexed_official_page() -> None:
    company_name = "深圳市和邦设计工程有限公司"
    mobile = "13823697171"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "cn.bing.com":
            return httpx.Response(
                200,
                text=f"""
                <li class="b_algo"><h2><a href="https://company.example/contact">
                联系我们 - {company_name}</a></h2>
                <p>{company_name}，别墅全案设计，热线：{mobile}。</p></li>
                """,
            )
        if request.url.host == "login.example":
            return httpx.Response(200, text="请登录后继续访问")
        return httpx.Response(200, text="<html></html>")

    provider = WebSearchProvider(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        request_interval_seconds=0,
    )

    evidence = provider.verify_contact_evidence(
        company_name=company_name,
        mobile=mobile,
        source_url="https://login.example/company",
        contact_person_name=None,
        contact_person_role=None,
    )

    assert evidence.status == "verified_official_page"
    assert evidence.evidence_url == "https://company.example/contact"
    assert company_name in evidence.evidence_quote
    assert mobile in evidence.evidence_quote


def test_cross_source_requires_two_distinct_domains() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="深圳市甲软装设计有限公司 业务咨询 13800138000",
            request=request,
        )

    provider = WebSearchProvider(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        request_interval_seconds=0,
    )

    evidence = provider.verify_contact_evidence(
        company_name="深圳市甲软装设计有限公司",
        mobile="13800138000",
        source_url=None,
        source_urls=[
            "https://directory-one.example/company",
            "https://directory-two.example/company",
        ],
        contact_person_name=None,
        contact_person_role=None,
    )

    assert evidence.status == "verified_cross_source"
    assert evidence.evidence_urls == [
        "https://directory-one.example/company",
        "https://directory-two.example/company",
    ]


def test_same_domain_exact_pair_is_verified_as_public_web() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="深圳市甲软装设计有限公司 业务咨询 13800138000",
            request=request,
        )

    provider = WebSearchProvider(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        request_interval_seconds=0,
    )

    evidence = provider.verify_contact_evidence(
        company_name="深圳市甲软装设计有限公司",
        mobile="13800138000",
        source_url=None,
        source_urls=[
            "https://www.qcc.com/firm/example",
            "https://www.qcc.com/contact/example",
        ],
        contact_person_name=None,
        contact_person_role=None,
    )

    assert evidence.status == "verified_public_web"
    assert evidence.evidence_url == "https://www.qcc.com/firm/example"
    assert "13800138000" in evidence.evidence_quote


def test_single_directory_exact_pair_is_verified_as_public_web() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="深圳市甲软装设计有限公司 业务咨询 13800138000",
            request=request,
        )

    provider = WebSearchProvider(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        request_interval_seconds=0,
    )

    evidence = provider.verify_contact_evidence(
        company_name="深圳市甲软装设计有限公司",
        mobile="13800138000",
        source_url="https://www.qcc.com/firm/example",
        contact_person_name=None,
        contact_person_role=None,
    )

    assert evidence.status == "verified_public_web"
    assert evidence.evidence_url == "https://www.qcc.com/firm/example"
    assert evidence.evidence_urls == ["https://www.qcc.com/firm/example"]
    assert "深圳市甲软装设计有限公司" in evidence.evidence_quote
    assert "13800138000" in evidence.evidence_quote


def test_matching_doubao_quote_without_fetched_page_is_unverified() -> None:
    provider = WebSearchProvider(
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: (_ for _ in ()).throw(
                    AssertionError(f"unexpected request: {request.url}")
                )
            )
        ),
        request_interval_seconds=0,
    )

    evidence = provider.verify_contact_evidence(
        company_name="深圳市甲软装设计有限公司",
        mobile="13800138000",
        source_url=None,
        source_urls=[],
        contact_person_name=None,
        contact_person_role=None,
    )

    assert evidence.status == "unverified_model_only"


def test_login_page_becomes_pending_login_source() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="请登录后继续访问 手机验证码登录", request=request)

    provider = WebSearchProvider(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        request_interval_seconds=0,
    )

    evidence = provider.verify_contact_evidence(
        company_name="深圳市甲软装设计有限公司",
        mobile="13800138000",
        source_url="https://directory.example/login",
        contact_person_name=None,
        contact_person_role=None,
    )

    assert evidence.status == "pending_login_source"


def test_conflicting_mobiles_are_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        mobile = "13800138000" if request.url.host == "one.example" else "13900139000"
        return httpx.Response(
            200,
            text=f"深圳市甲软装设计有限公司 业务咨询 {mobile}",
            request=request,
        )

    provider = WebSearchProvider(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        request_interval_seconds=0,
    )

    evidence = provider.verify_contact_evidence(
        company_name="深圳市甲软装设计有限公司",
        mobile="13800138000",
        source_url=None,
        source_urls=["https://one.example/contact", "https://two.example/contact"],
        contact_person_name=None,
        contact_person_role=None,
    )

    assert evidence.status == "rejected_mismatch"


def test_company_name_evidence_accepts_one_page_with_name_city_and_business() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host in {"www.sogou.com", "www.so.com", "cn.bing.com"}:
            return httpx.Response(
                200,
                text="""
                <li class="res-list"><h3><a href="https://company.example/about">
                深圳市甲软装设计有限公司</a></h3><p>深圳别墅全案软装设计</p></li>
                """,
            )
        return httpx.Response(
            200,
            text="深圳市甲软装设计有限公司位于深圳市，专注别墅全案软装设计服务。",
        )

    provider = WebSearchProvider(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        request_interval_seconds=0,
    )

    evidence = provider.verify_company_name(
        company_name="深圳市甲软装设计有限公司",
        city="深圳市",
        keywords=["软装公司", "全案设计公司"],
    )

    assert evidence == CompanyNameEvidence(
        status="verified_official_page",
        evidence_urls=["https://company.example/about"],
        reason="单一公开网页同时确认完整公司名称、城市和主营方向",
        city_matched=True,
        keyword_matched=True,
    )


def test_company_name_evidence_checks_supplied_source_when_search_is_unavailable() -> None:
    source_url = "https://company.example/about"

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == source_url:
            return httpx.Response(
                200,
                text="深圳市甲软装设计有限公司位于深圳市，专注别墅全案软装设计服务。",
            )
        return httpx.Response(503, text="search unavailable")

    provider = WebSearchProvider(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        request_interval_seconds=0,
        retry_limit=0,
    )

    evidence = provider.verify_company_name(
        company_name="深圳市甲软装设计有限公司",
        city="深圳市",
        keywords=["软装公司", "全案设计公司"],
        source_urls=[source_url],
    )

    assert evidence.status == "verified_official_page"
    assert evidence.evidence_urls == [source_url]


def test_company_name_evidence_normalizes_unicode_hyphen_in_source_url() -> None:
    supplied_url = "https://www.example‑design.com/about"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.example-design.com":
            return httpx.Response(
                200,
                text="深圳市甲空间设计有限公司位于深圳市，提供别墅全案设计服务。",
            )
        return httpx.Response(503, text="search unavailable")

    provider = WebSearchProvider(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        request_interval_seconds=0,
        retry_limit=0,
    )

    evidence = provider.verify_company_name(
        company_name="深圳市甲空间设计有限公司",
        city="深圳市",
        keywords=["全案设计公司"],
        source_urls=[supplied_url],
    )

    assert evidence.status == "verified_official_page"
    assert evidence.evidence_urls == ["https://www.example-design.com/about"]


def test_company_name_evidence_uses_bing_when_other_searches_are_empty() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "cn.bing.com":
            return httpx.Response(
                200,
                text="""
                <li class="b_algo"><h2><a href="https://company.example/about">
                深圳市和邦设计工程有限公司</a></h2>
                <p>深圳市和邦设计工程有限公司位于深圳，专注别墅全案设计和软装。</p></li>
                """,
            )
        if request.url.host == "company.example":
            return httpx.Response(
                200,
                text="深圳市和邦设计工程有限公司位于深圳市，专注别墅全案设计和软装。",
            )
        return httpx.Response(200, text="<html></html>")

    provider = WebSearchProvider(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        request_interval_seconds=0,
    )

    evidence = provider.verify_company_name(
        company_name="深圳市和邦设计工程有限公司",
        city="深圳市",
        keywords=["全案设计公司", "软装公司"],
    )

    assert evidence.status == "verified_official_page"
    assert evidence.evidence_urls == ["https://company.example/about"]


def test_company_name_evidence_accepts_two_distinct_domains_with_combined_signals() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host in {"www.sogou.com", "www.so.com"}:
            return httpx.Response(
                200,
                text="""
                <li class="res-list"><h3><a href="https://registry.example/profile">深圳市乙空间设计有限公司</a></h3><p>深圳市企业介绍</p></li>
                <li class="res-list"><h3><a href="https://design.example/case">深圳市乙空间设计有限公司</a></h3><p>别墅全案设计案例</p></li>
                """,
            )
        if request.url.host == "registry.example":
            return httpx.Response(200, text="深圳市乙空间设计有限公司注册于深圳市。")
        return httpx.Response(200, text="深圳市乙空间设计有限公司提供别墅全案设计服务。")

    provider = WebSearchProvider(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        request_interval_seconds=0,
    )

    evidence = provider.verify_company_name(
        company_name="深圳市乙空间设计有限公司",
        city="深圳市",
        keywords=["全案设计公司"],
    )

    assert evidence.status == "verified_cross_source"
    assert evidence.evidence_urls == [
        "https://registry.example/profile",
        "https://design.example/case",
    ]
    assert evidence.city_matched is True
    assert evidence.keyword_matched is True


def test_company_name_evidence_rejects_similar_name_and_conflicting_city() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host in {"www.sogou.com", "www.so.com"}:
            return httpx.Response(
                200,
                text="""
                <li class="res-list"><h3><a href="https://similar.example/about">深圳甲软装设计有限公司</a></h3><p>深圳软装设计</p></li>
                <li class="res-list"><h3><a href="https://wrong-city.example/about">深圳市甲软装设计有限公司</a></h3><p>广州软装设计公司</p></li>
                """,
            )
        if request.url.host == "similar.example":
            return httpx.Response(200, text="深圳甲软装设计有限公司位于深圳，提供软装设计。")
        return httpx.Response(200, text="深圳市甲软装设计有限公司位于广州市，提供软装设计。")

    provider = WebSearchProvider(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        request_interval_seconds=0,
    )

    evidence = provider.verify_company_name(
        company_name="深圳市甲软装设计有限公司",
        city="深圳市",
        keywords=["软装公司"],
    )

    assert evidence.status == "rejected"
    assert evidence.evidence_urls == ["https://wrong-city.example/about"]
    assert evidence.city_matched is False


def test_company_name_evidence_accepts_exact_public_search_summary() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host in {"www.sogou.com", "www.so.com"}:
            return httpx.Response(
                200,
                text="""
                <li class="res-list"><h3><a href="https://unrelated.example/about">
                深圳市甲软装设计有限公司</a></h3><p>深圳软装设计公司</p></li>
                """,
            )
        return httpx.Response(200, text="深圳市其他空间设计公司位于深圳，提供软装设计服务。")

    provider = WebSearchProvider(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        request_interval_seconds=0,
    )

    evidence = provider.verify_company_name(
        company_name="深圳市甲软装设计有限公司",
        city="深圳市",
        keywords=["软装公司"],
    )

    assert evidence.status == "verified_public_web"
    assert evidence.evidence_urls == ["https://unrelated.example/about"]
    assert evidence.city_matched is True
    assert evidence.keyword_matched is True


def test_company_name_evidence_records_total_search_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="temporarily unavailable")

    provider = WebSearchProvider(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        request_interval_seconds=0,
        retry_limit=0,
    )

    evidence = provider.verify_company_name(
        company_name="深圳市甲软装设计有限公司",
        city="深圳市",
        keywords=["软装公司"],
    )

    assert evidence.status == "verification_failed"
    assert evidence.evidence_urls == []


def test_bing_parser_keeps_outer_result_open_across_nested_divs() -> None:
    parser = _SearchResultParser()
    parser.feed(
        """
        <li class="b_algo"><div class="b_tpcn"><span>来源</span></div>
        <h2><a href="https://company.example/contact">武汉真实软装有限公司</a></h2>
        <div class="b_caption"><p>软装服务 13800138000</p></div></li>
        """
    )

    assert parser.results == [{
        "url": "https://company.example/contact",
        "title": "武汉真实软装有限公司",
        "snippet": "来源 软装服务 13800138000",
    }]


def test_360_parser_closes_result_with_void_image_tags() -> None:
    parser = _SearchResultParser()
    parser.feed(
        """
        <li class="res-list"><h3><a href="https://company.example/contact">
        武汉真实软装有限公司</a></h3><div><img src="logo.jpg">
        <p>武汉软装服务 13800138000</p></div></li>
        """
    )

    assert len(parser.results) == 1
    assert parser.results[0]["title"] == "武汉真实软装有限公司"


def test_html_text_and_search_snippet_exclude_executable_content() -> None:
    html = """
    <style>.button { height: auto; }</style>
    <script>window.monitor_mapLog({ type: 'close' });</script>
    <noscript>enable javascript</noscript>
    <svg><text>icon text</text></svg>
    <p>提供全案软装、空间设计和家具配置服务</p>
    """
    parser = _SearchResultParser()
    parser.feed(
        '<li class="res-list"><h3><a href="https://company.example">武汉真实软装有限公司</a></h3>'
        f"{html}</li>"
    )

    assert _visible_text(html) == "提供全案软装、空间设计和家具配置服务"
    assert parser.results[0]["snippet"] == "提供全案软装、空间设计和家具配置服务"


def test_service_intro_removes_urls_code_and_platform_boilerplate() -> None:
    intro = clean_service_intro(
        "window.monitor_mapLog(); .button{height:auto;} 简介:提供全案软装和空间设计服务 "
        "https://example.com shuidi.cn qq.com 027-88886666 取消立刻核查 我要催收债款 爱企查反馈",
        contacts=[],
        company_name="",
    )

    assert intro == "提供全案软装和空间设计服务"


def test_service_intro_removes_uncommon_symbol_garbling() -> None:
    assert clean_service_intro("⼀提供软装设计和家具配置服务", contacts=[], company_name="") == (
        "提供软装设计和家具配置服务"
    )


def test_company_name_cleaning_rejects_labels_and_keeps_company_subject() -> None:
    assert clean_company_name("闺色软装") == ""
    assert clean_company_name("小布匠全案软装") == ""
    assert clean_company_name("搜了网石河子本地装修公司") == ""
    assert clean_company_name("武汉软装公司推荐排行榜") == ""
    assert clean_company_name("武汉专业软装公司") == ""
    assert clean_company_name("武汉最好的软装公司") == ""
    assert (
        clean_company_name("窗帘_墙纸_灯饰厂家-武汉宝蓝软装设计公司")
        == "武汉宝蓝软装设计公司"
    )


def test_service_intro_removes_contact_and_company_noise() -> None:
    intro = clean_service_intro(
        "联系人：邹，电话：13597846156 武汉宝蓝软装设计公司主营产品包括窗帘、墙纸和软装设计",
        contacts=["13597846156"],
        company_name="武汉宝蓝软装设计公司",
    )

    assert intro == "主营产品包括窗帘、墙纸和软装设计"


def test_web_search_collects_only_source_linked_public_contacts() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.sogou.com" and request.url.path == "/web":
            assert "武汉" in request.url.params["query"]
            assert "软装公司" in request.url.params["query"]
            return httpx.Response(
                200,
                text="""
                <html><body>
                  <div class="vrwrap"><h3><a href="/link?url=company">武汉真实软装有限公司 - 联系我们</a></h3><p>专业全案软装服务</p></div>
                  <div class="vrwrap"><h3><a href="https://empty.example/about">武汉无联系方式公司</a></h3><p>企业介绍</p></div>
                </body></html>
                """,
            )
        if request.url.host == "www.sogou.com" and request.url.path == "/link":
            return httpx.Response(302, headers={"Location": "https://company.example/contact"})
        if request.url.host == "company.example":
            return httpx.Response(
                200,
                text="""
                <html><body>业务咨询 13800138000，邮箱 sales@company.example，微信：whsoft2026</body></html>
                """,
            )
        return httpx.Response(200, text="<html><body>没有公开联系方式</body></html>")

    provider = WebSearchProvider(
        client=httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True),
        max_results=5,
        request_interval_seconds=0,
    )

    leads = provider.collect(city="武汉市", keyword="软装公司", request_text="寻找家具 B 端客户")

    assert len(leads) == 1
    assert leads[0]["company_name"] == "武汉真实软装有限公司"
    assert leads[0]["mobile"] == "13800138000"
    assert leads[0]["email"] == "sales@company.example"
    assert leads[0]["wechat"] == "whsoft2026"
    assert leads[0]["source_url"] == "https://company.example/contact"
    assert leads[0]["matched_keyword"] == "软装公司"
    assert leads[0]["source_confidence"] == 50


def test_web_search_follows_same_domain_contact_page() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host in {"www.sogou.com", "www.so.com"}:
            return httpx.Response(
                200,
                text="""
                <li class="res-list"><h3><a href="https://company.example/about">
                武汉深度软装设计有限公司</a></h3><p>武汉全案软装设计服务</p></li>
                """,
            )
        if request.url.path == "/about":
            return httpx.Response(200, text='<p>企业介绍</p><a href="/contact-us">联系我们</a>')
        if request.url.path == "/contact-us":
            return httpx.Response(200, text="业务手机 13800138000")
        return httpx.Response(404)

    provider = WebSearchProvider(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        request_interval_seconds=0,
        max_search_pages=1,
    )

    leads = provider.collect(city="武汉市", keyword="软装公司", request_text="寻找客户")

    assert len(leads) == 1
    assert leads[0]["mobile"] == "13800138000"
    assert leads[0]["source_url"] == "https://company.example/contact-us"


def test_web_search_expands_queries_until_later_mobile_result() -> None:
    queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host in {"www.sogou.com", "www.so.com"}:
            query = request.url.params.get("query") or request.url.params.get("q") or ""
            queries.append(query)
            if "业务咨询 手机" in query:
                return httpx.Response(
                    200,
                    text="""
                    <li class="res-list"><h3><a href="https://mobile.example/contact">
                    武汉增量软装有限公司</a></h3><p>业务手机 13900139000</p></li>
                    """,
                )
            return httpx.Response(200, text="<html><body>没有结构化结果</body></html>")
        return httpx.Response(200, text="业务手机 13900139000")

    provider = WebSearchProvider(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        request_interval_seconds=0,
        max_search_pages=1,
    )

    leads = provider.collect(city="武汉市", keyword="软装公司", request_text="寻找客户")

    assert any("业务咨询 手机" in query for query in queries)
    assert {lead["company_name"] for lead in leads} == {"武汉增量软装有限公司"}


def test_web_search_deduplicates_and_fetches_candidate_pages_concurrently() -> None:
    lock = Lock()
    active = 0
    max_active = 0
    detail_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, max_active, detail_calls
        if request.url.host in {"www.sogou.com", "www.so.com", "cn.bing.com"}:
            return httpx.Response(
                200,
                text="".join(
                    f'<li class="res-list"><h3><a href="https://company{index}.example/about">'
                    f'武汉并发软装{index}有限公司</a></h3><p>武汉软装设计服务</p></li>'
                    for index in range(3)
                ),
            )
        with lock:
            detail_calls += 1
            active += 1
            max_active = max(max_active, active)
        sleep(0.03)
        with lock:
            active -= 1
        index = int((request.url.host or "company0").removeprefix("company").split(".")[0])
        return httpx.Response(200, text=f"武汉软装设计服务 1380013800{index}")

    provider = WebSearchProvider(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        request_interval_seconds=0,
        max_results=3,
        max_search_pages=1,
        detail_workers=3,
    )

    leads = provider.collect(city="武汉市", keyword="软装公司", request_text="寻找客户")

    assert len(leads) == 3
    assert detail_calls == 3
    assert max_active >= 2


def test_web_search_falls_back_to_360_when_sogou_has_no_structured_results() -> None:
    search_hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.sogou.com":
            search_hosts.append(request.url.host)
            return httpx.Response(200, text="<html><body>访问过于频繁，请稍后再试</body></html>")
        if request.url.host == "www.so.com":
            search_hosts.append(request.url.host)
            assert request.url.path == "/s"
            return httpx.Response(
                200,
                text="""
                <ol><li class="res-list"><h3><a href="https://bing-company.example/contact">
                武汉可信软装设计有限公司</a></h3><p>武汉全案软装服务，手机 13800138000</p></li></ol>
                """,
            )
        if request.url.host == "bing-company.example":
            return httpx.Response(200, text="武汉全案软装服务，业务手机 13800138000")
        return httpx.Response(404)

    provider = WebSearchProvider(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        request_interval_seconds=0,
    )

    leads = provider.collect(city="武汉市", keyword="软装公司", request_text="寻找客户")

    assert len(leads) == 1
    assert search_hosts[:2] == ["www.sogou.com", "www.so.com"]
    assert leads[0]["matched_keyword"] == "软装公司"
    assert leads[0]["source_confidence"] == 50


def test_web_search_excludes_results_without_keyword_or_keyword_core() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.sogou.com":
            return httpx.Response(
                200,
                text="""
                <div class="vrwrap"><h3><a href="https://unrelated.example/contact">
                武汉办公家具有限公司</a></h3><p>办公家具销售 13800138000</p></div>
                """,
            )
        if request.url.host == "unrelated.example":
            return httpx.Response(200, text="办公家具销售，手机 13800138000")
        return httpx.Response(404)

    provider = WebSearchProvider(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        request_interval_seconds=0,
    )

    assert provider.collect(city="武汉市", keyword="软装公司", request_text="寻找客户") == []


def test_web_search_reads_multiple_360_pages_for_more_results() -> None:
    offsets: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.sogou.com":
            return httpx.Response(200, text="<html>访问频繁</html>")
        if request.url.host == "www.so.com":
            offset = request.url.params.get("pn", "1")
            offsets.append(offset)
            suffix = "甲" if offset == "1" else "乙"
            mobile = "13800138000" if offset == "1" else "13900139000"
            return httpx.Response(
                200,
                text=f'<li class="res-list"><h3><a href="https://{suffix}.example/contact">武汉{suffix}软装有限公司</a></h3><p>软装服务 {mobile}</p></li>',
            )
        if request.url.host and request.url.host.endswith(".example"):
            return httpx.Response(200, text="武汉软装服务 13800138000 13900139000")
        return httpx.Response(404)

    provider = WebSearchProvider(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        request_interval_seconds=0,
        max_search_pages=2,
    )

    leads = provider.collect(city="武汉市", keyword="软装公司", request_text="寻找客户")

    assert {"1", "2"} <= set(offsets)
    assert {lead["company_name"] for lead in leads} == {"武汉甲软装有限公司", "武汉乙软装有限公司"}


def test_web_search_rejects_landline_only_pages() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.sogou.com":
            return httpx.Response(
                200,
                text='<div class="vrwrap"><h3><a href="https://company.example/contact">座机公司</a></h3></div>',
            )
        return httpx.Response(200, text="联系电话：027-88886666")

    provider = WebSearchProvider(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        request_interval_seconds=0,
    )

    assert provider.collect(city="武汉市", keyword="软装公司", request_text="寻找客户") == []


def test_web_search_uses_result_snippet_as_contact_evidence() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.sogou.com":
            return httpx.Response(
                200,
                text="""
                <div class="vrwrap"><h3><a href="https://company.example/about">武汉邮箱软装公司</a></h3>
                <p>公开邮箱 public@company.example</p></div>
                """,
            )
        return httpx.Response(200, text="企业介绍页面")

    provider = WebSearchProvider(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_results=0,
        request_interval_seconds=0,
    )

    leads = provider.collect(city="武汉市", keyword="软装公司", request_text="寻找客户")

    assert len(leads) == 1
    assert leads[0]["email"] == "public@company.example"
    assert leads[0]["source_url"] == "https://company.example/about"


def test_web_search_uses_chinese_page_sentence_when_snippet_is_empty() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host in {"www.sogou.com", "www.so.com"}:
            return httpx.Response(
                200,
                text="""
                <li class="res-list"><h3><a href="https://company.example/about">
                武汉中文软装有限公司</a></h3><script>window.monitor_mapLog()</script></li>
                """,
            )
        return httpx.Response(200, text="武汉中文软装有限公司专业提供全案软装、空间设计和家具配置服务，手机 13800138000。")

    provider = WebSearchProvider(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        request_interval_seconds=0,
        max_search_pages=1,
    )

    leads = provider.collect(city="武汉市", keyword="软装公司", request_text="寻找客户")

    assert leads[0]["service_intro"] == "专业提供全案软装、空间设计和家具配置服务"


def test_web_search_keeps_contact_snippet_when_company_page_is_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.sogou.com":
            return httpx.Response(
                200,
                text="""
                <div class="vrwrap"><h3><a href="https://blocked.example/contact">
                武汉公开软装有限公司</a></h3><p>武汉软装服务，业务手机 13800138000</p></div>
                """,
            )
        return httpx.Response(503, text="temporarily unavailable")

    provider = WebSearchProvider(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        request_interval_seconds=0,
        retry_limit=0,
    )

    leads = provider.collect(city="武汉市", keyword="软装公司", request_text="寻找客户")

    assert len(leads) == 1
    assert leads[0]["company_name"] == "武汉公开软装有限公司"
    assert leads[0]["mobile"] == "13800138000"
    assert leads[0]["source_url"] == "https://blocked.example/contact"
    assert leads[0]["source_confidence"] == 50


def test_web_search_cleans_company_name_title_noise() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.sogou.com":
            return httpx.Response(
                200,
                text="""
                <div class="vrwrap"><h3><a href="https://company.example/contact">
                联系方式 - 武汉泊菲特软装设计有限公司电话号码</a></h3></div>
                """,
            )
        return httpx.Response(200, text="咨询手机 13800138000")

    provider = WebSearchProvider(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        request_interval_seconds=0,
    )

    leads = provider.collect(city="武汉市", keyword="软装公司", request_text="寻找客户")

    assert leads[0]["company_name"] == "武汉泊菲特软装设计有限公司"


def test_web_search_retries_rate_limit_responses() -> None:
    search_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal search_calls
        if request.url.host == "www.sogou.com":
            search_calls += 1
            if search_calls == 1:
                return httpx.Response(429, text="rate limited")
            return httpx.Response(200, text="<html></html>")
        return httpx.Response(404)

    provider = WebSearchProvider(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_results=0,
        request_interval_seconds=0,
        retry_limit=1,
    )

    with pytest.raises(ProviderError, match="no structured results"):
        provider.collect(city="武汉市", keyword="软装公司", request_text="寻找客户")
    assert search_calls == 9
