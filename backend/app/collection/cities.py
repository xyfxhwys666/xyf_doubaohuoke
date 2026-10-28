import json
from pathlib import Path


def tier3_plus_city_names() -> list[str]:
    path = Path(__file__).parent / "data" / "tier3_plus_cities.json"
    values = json.loads(path.read_text(encoding="utf-8"))
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))
