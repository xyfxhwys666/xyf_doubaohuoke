from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy import select

from app.api.deps import DbSession, get_current_user
from app.models.identity import User, normalize_email
from app.security.passwords import verify_password
from app.security.sessions import SESSION_COOKIE_NAME, SESSION_TTL, create_session_token

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class LoginRequest(BaseModel):
    email: str
    password: str

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        return normalize_email(value)


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str


@router.post("/login", response_model=UserResponse)
def login(payload: LoginRequest, request: Request, response: Response, db: DbSession) -> User:
    user = db.scalar(
        select(User).where(User.email == normalize_email(str(payload.email)), User.is_active.is_(True))
    )
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")
    settings = request.app.state.settings
    response.set_cookie(
        SESSION_COOKIE_NAME,
        create_session_token(user.id, settings.session_secret.get_secret_value()),
        httponly=True,
        secure=settings.app_env == "production",
        samesite="lax",
        max_age=int(SESSION_TTL.total_seconds()),
        path="/",
    )
    return user


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE_NAME, httponly=True, samesite="lax", path="/")


@router.get("/me", response_model=UserResponse)
def me(user: Annotated[User, Depends(get_current_user)]) -> User:
    return user
