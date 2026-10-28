from secrets import token_urlsafe

import httpx
from cryptography.fernet import Fernet

from app.collection.providers import AmapProvider, build_providers
from app.config import Settings


def test_amap_city_catalog_returns_prefecture_level_cities() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v3/config/district"
        return httpx.Response(
            200,
            json={
                "status": "1",
                "districts": [
                    {
                        "name": "中华人民共和国",
                        "level": "country",
                        "districts": [
                            {
                                "name": "湖北省",
                                "level": "province",
                                "districts": [
                                    {"name": "武汉市", "level": "city", "adcode": "420100", "districts": []},
                                    {"name": "仙桃市", "level": "district", "adcode": "429004", "districts": []},
                                ],
                            },
                            {
                                "name": "北京市",
                                "level": "province",
                                "districts": [
                                    {"name": "北京市", "level": "city", "adcode": "110000", "districts": []}
                                ],
                            },
                        ],
                    }
                ],
            },
        )

    provider = AmapProvider(token_urlsafe(24), client=httpx.Client(transport=httpx.MockTransport(handler)))

    assert provider.list_cities() == ["北京市", "武汉市"]


def test_amap_collection_keeps_only_mobile_contacts_and_source_urls() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v3/place/text"
        return httpx.Response(
            200,
            json={
                "status": "1",
                "count": "3",
                "pois": [
                    {
                        "name": "武汉真实软装有限公司",
                        "tel": "027-88886666;13800138000",
                        "address": "武汉市江汉区测试路 1 号",
                        "type": "购物服务;家居建材市场;家具城",
                        "location": "114.3000,30.5000",
                    },
                    {
                        "name": "武汉仅座机公司",
                        "tel": "027-12345678",
                        "address": "武汉市武昌区测试路 2 号",
                        "type": "公司企业",
                        "location": "114.3200,30.5200",
                    },
                    {
                        "name": "武汉公开邮箱软装公司",
                        "tel": "027-87654321",
                        "email": "HELLO@SOFT.EXAMPLE",
                        "address": "武汉市洪山区测试路 3 号",
                        "type": "公司企业",
                        "location": "114.3300,30.5300",
                    },
                ],
            },
        )

    secret = token_urlsafe(24)
    provider = AmapProvider(secret, client=httpx.Client(transport=httpx.MockTransport(handler)))

    leads = provider.collect(city="武汉市", keyword="软装公司", request_text="家具 B 端客户")

    assert len(leads) == 2
    assert leads[0]["company_name"] == "武汉真实软装有限公司"
    assert leads[0]["mobile"] == "13800138000"
    assert leads[0]["service_intro"] == "购物服务;家居建材市场;家具城"
    assert leads[0]["source_url"].startswith("https://uri.amap.com/marker?")
    assert secret not in leads[0]["source_url"]
    assert leads[1]["email"] == "HELLO@SOFT.EXAMPLE"
    assert leads[1]["mobile"] is None


def test_amap_retries_qps_limit_errors() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                json={"status": "0", "info": "CUQPS_HAS_EXCEEDED_THE_LIMIT", "infocode": "10021"},
            )
        return httpx.Response(200, json={"status": "1", "count": "0", "pois": []})

    provider = AmapProvider(
        token_urlsafe(24),
        request_interval_seconds=0,
        retry_limit=1,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert provider.collect(city="武汉市", keyword="软装公司", request_text="家具 B 端客户") == []
    assert calls == 2


def test_provider_registry_excludes_demo_and_direct_llm_sources(tmp_path) -> None:
    settings = Settings(
        app_env="test",
        database_url="sqlite+pysqlite:///:memory:",
        app_master_key=Fernet.generate_key().decode("ascii"),
        session_secret=token_urlsafe(32),
        data_dir=tmp_path,
        amap_api_key=token_urlsafe(24),
    )

    assert list(build_providers(settings)) == ["web_search"]
