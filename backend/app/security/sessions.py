from datetime import UTC, datetime, timedelta
from uuid import UUID

import jwt

SESSION_COOKIE_NAME = "session"
SESSION_TTL = timedelta(minutes=30)
SESSION_ALGORITHM = "HS256"


def create_session_token(user_id: UUID, secret: str, *, ttl: timedelta = SESSION_TTL) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + ttl,
        "purpose": "browser-session",
    }
    return jwt.encode(payload, secret, algorithm=SESSION_ALGORITHM)


def decode_session_token(token: str, secret: str) -> UUID:
    payload = jwt.decode(
        token,
        secret,
        algorithms=[SESSION_ALGORITHM],
        options={"require": ["sub", "iat", "exp", "purpose"]},
    )
    if payload["purpose"] != "browser-session":
        raise jwt.InvalidTokenError("invalid token purpose")
    try:
        return UUID(payload["sub"])
    except (TypeError, ValueError) as exc:
        raise jwt.InvalidTokenError("invalid token subject") from exc
