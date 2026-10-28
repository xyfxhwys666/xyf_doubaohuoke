from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "production"
    database_url: str = "sqlite:///./data/identity.db"
    app_master_key: SecretStr
    session_secret: SecretStr
    data_dir: Path = Path(__file__).resolve().parents[2] / "data"
    amap_api_key: SecretStr | None = None
    amap_base_url: str = "https://restapi.amap.com"
    amap_max_pages: int = 3
    amap_request_interval_seconds: float = 0.3
    amap_retry_limit: int = 3
    web_search_max_results: int = 10
    web_search_max_pages: int = 2
    web_search_request_interval_seconds: float = 0.4
    web_search_contact_verification_timeout_seconds: float = 45.0
    web_search_max_contact_source_urls: int = 4
    doubao_api_key: SecretStr | None = None
    doubao_base_url: str | None = "https://ark.cn-beijing.volces.com/api/v3"
    doubao_model: str | None = None
    doubao_relay_api_key: SecretStr | None = None
    doubao_relay_base_url: str | None = None
    doubao_relay_model: str | None = None
    doubao_web_enabled: bool = False
    doubao_web_profile_dir: Path = Path("./data/doubao-browser-profile")
    doubao_web_chat_url: str = "https://www.doubao.com/chat/"
    doubao_web_cdp_host: str = "127.0.0.1"
    doubao_web_cdp_port: int = 9333
    doubao_web_browser_executable: Path | None = None
    doubao_web_request_timeout_seconds: int = 180
    doubao_web_discovery_timeout_seconds: int = 90
    doubao_web_min_interval_seconds: float = 45.0
    doubao_web_max_interval_seconds: float = 90.0
    doubao_web_batch_min_size: int = 8
    doubao_web_batch_max_size: int = 12
    doubao_web_cooldown_min_seconds: float = 2700.0
    doubao_web_cooldown_max_seconds: float = 3600.0
    doubao_web_attention_poll_seconds: float = 90.0
    doubao_web_ready_confirmations: int = 3
    deepseek_api_key: SecretStr | None = None
    deepseek_base_url: str | None = "https://api.deepseek.com"
    deepseek_model: str | None = "deepseek-v4-flash"
    erp_auto_push: bool = False
    erp_delivery_url: str | None = None
    erp_delivery_timeout_seconds: float = 30.0
    collection_max_concurrent_tasks: int = 2
    legacy_collection_owner_id: str | None = None
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
