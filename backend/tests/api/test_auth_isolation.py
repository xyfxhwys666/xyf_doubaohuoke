from sqlalchemy import select

from app.models.identity import WorkspaceMembership


def test_member_can_read_own_workspace(client, workspace_factory, login_as) -> None:
    first = workspace_factory()
    login_as(first.operator)
    response = client.get(f"/api/v1/workspaces/{first.id}")
    assert response.status_code == 200
    assert response.json()["id"] == str(first.id)


def test_member_cannot_read_another_workspace(client, workspace_factory, login_as) -> None:
    first = workspace_factory()
    second = workspace_factory()
    login_as(first.operator)
    response = client.get(f"/api/v1/workspaces/{second.id}")
    assert response.status_code == 404


def test_inactive_workspace_returns_not_found(client, workspace_factory, login_as, db_session) -> None:
    first = workspace_factory()
    login_as(first.operator)
    first.workspace.is_active = False
    db_session.flush()
    response = client.get(f"/api/v1/workspaces/{first.id}")
    assert response.status_code == 404


def test_inactive_membership_returns_not_found(client, workspace_factory, login_as, db_session) -> None:
    first = workspace_factory()
    login_as(first.operator)
    membership = db_session.scalar(
        select(WorkspaceMembership).where(
            WorkspaceMembership.workspace_id == first.id,
            WorkspaceMembership.user_id == first.operator.id,
        )
    )
    assert membership is not None
    membership.is_active = False
    db_session.flush()
    response = client.get(f"/api/v1/workspaces/{first.id}")
    assert response.status_code == 404
