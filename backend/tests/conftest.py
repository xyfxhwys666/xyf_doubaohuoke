from collections.abc import Callable, Generator
from dataclasses import dataclass
from secrets import token_urlsafe
from uuid import UUID, uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from app.api.deps import require_roles
from app.config import Settings
from app.db import get_db
from app.main import create_app
from app.models.base import Base
from app.models.identity import User, Workspace, WorkspaceMembership, WorkspaceRole
from app.security.passwords import hash_password
from app.security.sessions import SESSION_COOKIE_NAME, create_session_token


def add_role_probe_routes(app: FastAPI) -> None:
    def endpoint_for(role: WorkspaceRole) -> Callable[[], dict[str, str]]:
        def endpoint() -> dict[str, str]:
            return {"role": role.value}

        return endpoint

    for role in WorkspaceRole:
        app.add_api_route(
            f"/_test/workspaces/{{workspace_id}}/roles/{role.value}",
            endpoint_for(role),
            methods=["GET"],
            dependencies=[Depends(require_roles(role))],
        )


@dataclass(frozen=True, slots=True)
class WorkspaceFixture:
    workspace: Workspace
    administrator: User
    operator: User
    reviewer: User
    read_only: User
    password: str

    @property
    def id(self) -> UUID:
        return self.workspace.id


@pytest.fixture(scope="session")
def settings(tmp_path_factory: pytest.TempPathFactory) -> Settings:
    root = tmp_path_factory.mktemp("app")
    return Settings(
        _env_file=None,
        app_env="test",
        database_url=f"sqlite+pysqlite:///{root / 'identity-test.db'}",
        app_master_key=Fernet.generate_key().decode("ascii"),
        session_secret=token_urlsafe(32),
        data_dir=root / "data",
        erp_auto_push=False,
    )


@pytest.fixture(scope="session")
def database_engine(settings: Settings) -> Generator[Engine, None, None]:
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def db_session(database_engine: Engine) -> Generator[Session, None, None]:
    with database_engine.connect() as connection:
        transaction = connection.begin()
        session = Session(
            bind=connection,
            autoflush=False,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            yield session
        finally:
            session.close()
            transaction.rollback()


@pytest.fixture
def client(settings: Settings, db_session: Session) -> Generator[TestClient, None, None]:
    app = create_app(settings)
    add_role_probe_routes(app)

    def override_db() -> Generator[Session, None, None]:
        yield db_session

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def workspace_factory(db_session: Session) -> Callable[[], WorkspaceFixture]:
    def factory() -> WorkspaceFixture:
        suffix = uuid4().hex
        password = token_urlsafe(24)
        password_hash = hash_password(password)
        workspace = Workspace(name=f"Workspace {suffix}")
        administrator = User(email=f"administrator-{suffix}@example.test", password_hash=password_hash)
        operator = User(email=f"operator-{suffix}@example.test", password_hash=password_hash)
        reviewer = User(email=f"reviewer-{suffix}@example.test", password_hash=password_hash)
        read_only = User(email=f"read-only-{suffix}@example.test", password_hash=password_hash)
        db_session.add_all([workspace, administrator, operator, reviewer, read_only])
        db_session.flush()
        db_session.add_all(
            [
                WorkspaceMembership(
                    workspace_id=workspace.id,
                    user_id=administrator.id,
                    role=WorkspaceRole.ADMINISTRATOR,
                ),
                WorkspaceMembership(
                    workspace_id=workspace.id,
                    user_id=operator.id,
                    role=WorkspaceRole.OPERATOR,
                ),
                WorkspaceMembership(
                    workspace_id=workspace.id,
                    user_id=reviewer.id,
                    role=WorkspaceRole.REVIEWER,
                ),
                WorkspaceMembership(
                    workspace_id=workspace.id,
                    user_id=read_only.id,
                    role=WorkspaceRole.READ_ONLY,
                ),
            ]
        )
        db_session.flush()
        return WorkspaceFixture(
            workspace=workspace,
            administrator=administrator,
            operator=operator,
            reviewer=reviewer,
            read_only=read_only,
            password=password,
        )

    return factory


@pytest.fixture
def login_as(client: TestClient, settings: Settings) -> Callable[[User], None]:
    def login(user: User) -> None:
        token = create_session_token(user.id, settings.session_secret.get_secret_value())
        client.cookies.set(SESSION_COOKIE_NAME, token)

    return login


@pytest.fixture
def administrator_client(
    client: TestClient,
    workspace_factory: Callable[[], WorkspaceFixture],
    login_as: Callable[[User], None],
) -> TestClient:
    login_as(workspace_factory().administrator)
    return client


@pytest.fixture
def operator_client(
    client: TestClient,
    workspace_factory: Callable[[], WorkspaceFixture],
    login_as: Callable[[User], None],
) -> TestClient:
    login_as(workspace_factory().operator)
    return client


@pytest.fixture
def reviewer_client(
    client: TestClient,
    workspace_factory: Callable[[], WorkspaceFixture],
    login_as: Callable[[User], None],
) -> TestClient:
    login_as(workspace_factory().reviewer)
    return client


@pytest.fixture
def read_only_client(
    client: TestClient,
    workspace_factory: Callable[[], WorkspaceFixture],
    login_as: Callable[[User], None],
) -> TestClient:
    login_as(workspace_factory().read_only)
    return client
