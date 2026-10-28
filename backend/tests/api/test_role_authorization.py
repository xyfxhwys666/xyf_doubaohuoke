import pytest

from app.models.identity import WorkspaceRole

ROLE_CASES = [
    ("administrator", WorkspaceRole.ADMINISTRATOR, WorkspaceRole.OPERATOR),
    ("operator", WorkspaceRole.OPERATOR, WorkspaceRole.REVIEWER),
    ("reviewer", WorkspaceRole.REVIEWER, WorkspaceRole.READ_ONLY),
    ("read_only", WorkspaceRole.READ_ONLY, WorkspaceRole.ADMINISTRATOR),
]


@pytest.mark.parametrize(("actor_name", "allowed_role", "denied_role"), ROLE_CASES)
def test_each_workspace_role_is_loaded_from_membership(
    client,
    workspace_factory,
    actor_name,
    allowed_role,
    denied_role,
) -> None:
    first = workspace_factory()
    actor = getattr(first, actor_name)
    login = client.post(
        "/api/v1/auth/login",
        json={"email": actor.email, "password": first.password},
    )
    assert login.status_code == 200

    allowed = client.get(f"/_test/workspaces/{first.id}/roles/{allowed_role.value}")
    denied = client.get(f"/_test/workspaces/{first.id}/roles/{denied_role.value}")
    assert allowed.status_code == 200
    assert denied.status_code == 403
