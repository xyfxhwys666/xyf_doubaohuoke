from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

import jwt
from fastapi import Cookie, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.identity import User, Workspace, WorkspaceMembership, WorkspaceRole
from app.security.sessions import SESSION_COOKIE_NAME, decode_session_token

DbSession = Annotated[Session, Depends(get_db)]


@dataclass(frozen=True, slots=True)
class CurrentPrincipal:
    user_id: UUID
    workspace_id: UUID
    roles: frozenset[WorkspaceRole]


def get_current_user(
    request: Request,
    db: DbSession,
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE_NAME)] = None,
) -> User:
    if session_cookie is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required")
    try:
        user_id = decode_session_token(session_cookie, request.app.state.settings.session_secret.get_secret_value())
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid session") from exc
    user = db.scalar(select(User).where(User.id == user_id, User.is_active.is_(True)))
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid session")
    return user


def require_collection_access(
    request: Request,
    db: DbSession,
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE_NAME)] = None,
) -> None:
    if request.app.state.settings.app_env in {"development", "test"}:
        request.state.collection_owner = "local-user"
        return
    request.state.collection_owner = str(
        get_current_user(request, db, session_cookie).id
    )


def get_current_principal(
    workspace_id: UUID,
    db: DbSession,
    user: Annotated[User, Depends(get_current_user)],
) -> CurrentPrincipal:
    membership = db.scalar(
        select(WorkspaceMembership)
        .join(Workspace, Workspace.id == WorkspaceMembership.workspace_id)
        .where(
            WorkspaceMembership.workspace_id == workspace_id,
            WorkspaceMembership.user_id == user.id,
            WorkspaceMembership.is_active.is_(True),
            Workspace.is_active.is_(True),
        )
    )
    if membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="workspace not found")
    return CurrentPrincipal(
        user_id=user.id,
        workspace_id=workspace_id,
        roles=frozenset({membership.role}),
    )


def require_roles(*roles: WorkspaceRole | str) -> Callable[[CurrentPrincipal], CurrentPrincipal]:
    required = frozenset(WorkspaceRole(role) for role in roles)

    def dependency(principal: Annotated[CurrentPrincipal, Depends(get_current_principal)]) -> CurrentPrincipal:
        if required.isdisjoint(principal.roles):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="insufficient role")
        return principal

    return dependency
