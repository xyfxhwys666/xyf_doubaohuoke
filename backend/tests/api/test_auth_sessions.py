from datetime import timedelta
from secrets import token_urlsafe

from app.security.sessions import SESSION_COOKIE_NAME, create_session_token


def test_real_login_accepts_case_variant_and_sets_http_only_cookie(client, workspace_factory) -> None:
    first = workspace_factory()
    response = client.post(
        "/api/v1/auth/login",
        json={"email": first.operator.email.upper(), "password": first.password},
    )
    assert response.status_code == 200
    assert response.json()["id"] == str(first.operator.id)
    assert "httponly" in response.headers["set-cookie"].lower()
    assert client.get("/api/v1/auth/me").status_code == 200


def test_login_rejects_invalid_password(client, workspace_factory) -> None:
    first = workspace_factory()
    response = client.post(
        "/api/v1/auth/login",
        json={"email": first.operator.email, "password": token_urlsafe(24)},
    )
    assert response.status_code == 401
    assert SESSION_COOKIE_NAME not in client.cookies


def test_logout_clears_session_cookie(client, workspace_factory) -> None:
    first = workspace_factory()
    client.post(
        "/api/v1/auth/login",
        json={"email": first.operator.email, "password": first.password},
    )
    response = client.post("/api/v1/auth/logout")
    assert response.status_code == 204
    assert SESSION_COOKIE_NAME not in client.cookies
    assert client.get("/api/v1/auth/me").status_code == 401


def test_invalid_session_is_rejected(client) -> None:
    client.cookies.set(SESSION_COOKIE_NAME, token_urlsafe(24))
    assert client.get("/api/v1/auth/me").status_code == 401


def test_expired_session_is_rejected(client, settings, workspace_factory) -> None:
    first = workspace_factory()
    expired = create_session_token(
        first.operator.id,
        settings.session_secret.get_secret_value(),
        ttl=-timedelta(seconds=1),
    )
    client.cookies.set(SESSION_COOKIE_NAME, expired)
    assert client.get("/api/v1/auth/me").status_code == 401


def test_inactive_user_session_is_rejected(client, workspace_factory, db_session) -> None:
    first = workspace_factory()
    login = client.post(
        "/api/v1/auth/login",
        json={"email": first.operator.email, "password": first.password},
    )
    assert login.status_code == 200
    first.operator.is_active = False
    db_session.flush()
    assert client.get("/api/v1/auth/me").status_code == 401
