from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from app.api.deps import CurrentPrincipal, DbSession, get_current_principal
from app.models.identity import Workspace

router = APIRouter(prefix="/api/v1/workspaces", tags=["workspaces"])


class WorkspaceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    created_at: datetime


@router.get("/{workspace_id}", response_model=WorkspaceResponse)
def get_workspace(
    workspace_id: UUID,
    db: DbSession,
    principal: Annotated[CurrentPrincipal, Depends(get_current_principal)],
) -> Workspace:
    workspace = db.scalar(
        select(Workspace).where(
            Workspace.id == principal.workspace_id,
            Workspace.id == workspace_id,
            Workspace.is_active.is_(True),
        )
    )
    if workspace is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="workspace not found")
    return workspace
