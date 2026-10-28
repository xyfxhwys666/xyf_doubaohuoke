from app.collection.cities import tier3_plus_city_names


def test_catalog_is_stable_and_contains_representative_city_tiers() -> None:
    cities = tier3_plus_city_names()

    assert len(cities) == len(set(cities))
    assert len(cities) >= 70
    assert {"北京市", "武汉市", "长沙市", "洛阳市", "绵阳市"} <= set(cities)
    assert all(city.endswith(("市", "自治州", "地区", "盟")) for city in cities)


def test_catalog_returns_a_copy() -> None:
    first = tier3_plus_city_names()
    first.pop()

    assert len(tier3_plus_city_names()) > len(first)
