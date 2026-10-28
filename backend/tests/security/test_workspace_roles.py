from secrets import token_urlsafe
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.models.identity import User, Workspace, WorkspaceRole
from app.security.passwords import hash_password


def test_workspace_role_domain_is_exact() -> None:
    assert {role.value for role in WorkspaceRole} == {
        "administrator",
        "operator",
        "reviewer",
        "read-only",
    }


def test_database_rejects_unknown_workspace_role(db_session) -> None:
    suffix = uuid4().hex
    workspace = Workspace(name=f"Workspace {suffix}")
    user = User(
        email=f"member-{suffix}@example.test",
        password_hash=hash_password(token_urlsafe(24)),
    )
    db_session.add_all([workspace, user])
    db_session.flush()

    with pytest.raises(IntegrityError):
        db_session.execute(
            text(
                """
                INSERT INTO workspace_memberships (id, workspace_id, user_id, role, is_active)
                VALUES (:id, :workspace_id, :user_id, :role, true)
                """
            ),
            {
                "id": uuid4().hex,
                "workspace_id": workspace.id.hex,
                "user_id": user.id.hex,
                "role": "owner",
            },
        )
