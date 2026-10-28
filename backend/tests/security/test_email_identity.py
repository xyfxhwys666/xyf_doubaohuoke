from secrets import token_urlsafe
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.identity import User
from app.security.passwords import hash_password


def test_user_email_is_normalized_on_write() -> None:
    suffix = uuid4().hex
    user = User(
        email=f"Operator-{suffix}@Example.TEST ",
        password_hash=hash_password(token_urlsafe(24)),
    )
    assert user.email == f"operator-{suffix}@example.test"


def test_case_variant_user_emails_cannot_coexist(db_session) -> None:
    email = f"operator-{uuid4().hex}@example.test"
    password_hash = hash_password(token_urlsafe(24))
    db_session.add(User(email=email, password_hash=password_hash))
    db_session.flush()
    db_session.add(User(email=email.upper(), password_hash=password_hash))
    with pytest.raises(IntegrityError):
        db_session.flush()
