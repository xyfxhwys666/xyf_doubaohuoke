from pathlib import Path

from cryptography.fernet import Fernet

from app.config import Settings


def test_erp_delivery_url_has_no_concrete_default() -> None:
    settings = Settings(
        _env_file=None,
        app_master_key=Fernet.generate_key().decode("ascii"),
        session_secret="test-session-secret",
    )

    assert settings.app_env == "production"
    assert settings.erp_delivery_url is None
    assert "APP_ENV=production" in Path(".env.example").read_text(encoding="utf-8")
    assert settings.legacy_collection_owner_id is None
    assert "LEGACY_COLLECTION_OWNER_ID=" in Path(".env.example").read_text(encoding="utf-8")
    assert "ERP_DELIVERY_URL=" in Path(".env.example").read_text(encoding="utf-8")
    assert "qingflow.com" not in Path(".env.example").read_text(encoding="utf-8")
    assert settings.doubao_web_enabled is False
    assert settings.doubao_web_chat_url == "https://www.doubao.com/chat/"
    assert settings.doubao_web_request_timeout_seconds == 180
    assert settings.doubao_web_min_interval_seconds == 45
    assert settings.doubao_web_max_interval_seconds == 90
    assert settings.doubao_web_batch_min_size == 8
    assert settings.doubao_web_batch_max_size == 12
    assert settings.doubao_web_cooldown_min_seconds == 2700
    assert settings.doubao_web_cooldown_max_seconds == 3600
    assert settings.doubao_web_attention_poll_seconds == 90
    assert settings.doubao_web_ready_confirmations == 3


def test_doubao_relay_settings_are_loaded_separately(monkeypatch) -> None:
    monkeypatch.setenv("DOUBAO_RELAY_API_KEY", "relay-secret")
    monkeypatch.setenv("DOUBAO_RELAY_BASE_URL", "https://relay.example.test/v1")
    monkeypatch.setenv("DOUBAO_RELAY_MODEL", "doubao-relay-model")

    settings = Settings(
        _env_file=None,
        app_master_key=Fernet.generate_key().decode("ascii"),
        session_secret="test-session-secret",
    )

    assert settings.doubao_relay_api_key is not None
    assert settings.doubao_relay_api_key.get_secret_value() == "relay-secret"
    assert settings.doubao_relay_base_url == "https://relay.example.test/v1"
    assert settings.doubao_relay_model == "doubao-relay-model"

    example = Path(".env.example").read_text(encoding="utf-8")
    assert "DOUBAO_RELAY_API_KEY=" in example
    assert "DOUBAO_RELAY_BASE_URL=" in example
    assert "DOUBAO_RELAY_MODEL=" in example
