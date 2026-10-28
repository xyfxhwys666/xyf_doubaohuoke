from typing import Any


def _payload(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "request_text": name,
        "cities": ["武汉市"],
        "keywords": ["软装公司"],
        "providers": ["web_search"],
        "models": [],
        "required_contacts": ["mobile"],
    }


def test_collection_routes_require_session_outside_demo(client, monkeypatch) -> None:
    monkeypatch.setattr(client.app.state.settings, "app_env", "production")

    response = client.get("/api/v1/collection-tasks")

    assert response.status_code == 401


def test_collection_tasks_are_scoped_to_authenticated_owner(
    client,
    workspace_factory,
    login_as,
    monkeypatch,
) -> None:
    monkeypatch.setattr(client.app.state.settings, "app_env", "production")
    first = workspace_factory()
    second = workspace_factory()
    service = client.app.state.collection_service
    own_task = service.create_task(_payload("first"), created_by=str(first.operator.id))
    other_task = service.create_task(_payload("second"), created_by=str(second.operator.id))
    login_as(first.operator)

    listed = client.get("/api/v1/collection-tasks")
    hidden = client.get(f"/api/v1/collection-tasks/{other_task['id']}")

    assert [task["id"] for task in listed.json()["items"]] == [own_task["id"]]
    assert hidden.status_code == 404


def test_production_collection_list_does_not_claim_legacy_tasks_without_owner_config(
    client,
    workspace_factory,
    login_as,
    monkeypatch,
) -> None:
    monkeypatch.setattr(client.app.state.settings, "app_env", "production")
    actor = workspace_factory().operator
    service = client.app.state.collection_service
    legacy_task = service.create_task(_payload("legacy"))
    login_as(actor)

    listed = client.get("/api/v1/collection-tasks")

    assert listed.json()["items"] == []
    assert service.get_task(legacy_task["id"])["created_by"] == "local-user"


def test_production_collection_list_claims_legacy_tasks_for_configured_owner(
    client,
    workspace_factory,
    login_as,
    monkeypatch,
) -> None:
    monkeypatch.setattr(client.app.state.settings, "app_env", "production")
    actor = workspace_factory().operator
    monkeypatch.setattr(
        client.app.state.settings,
        "legacy_collection_owner_id",
        str(actor.id),
    )
    service = client.app.state.collection_service
    legacy_task = service.create_task(_payload("legacy"))
    login_as(actor)

    listed = client.get("/api/v1/collection-tasks")

    assert legacy_task["id"] in {task["id"] for task in listed.json()["items"]}
    assert service.get_task(legacy_task["id"])["created_by"] == str(actor.id)


def test_production_collection_list_does_not_claim_for_another_authenticated_user(
    client,
    workspace_factory,
    login_as,
    monkeypatch,
) -> None:
    monkeypatch.setattr(client.app.state.settings, "app_env", "production")
    configured = workspace_factory().operator
    other = workspace_factory().operator
    monkeypatch.setattr(
        client.app.state.settings,
        "legacy_collection_owner_id",
        str(configured.id),
    )
    service = client.app.state.collection_service
    legacy_task = service.create_task(_payload("legacy"))
    login_as(other)

    listed = client.get("/api/v1/collection-tasks")

    assert listed.json()["items"] == []
    assert service.get_task(legacy_task["id"])["created_by"] == "local-user"
