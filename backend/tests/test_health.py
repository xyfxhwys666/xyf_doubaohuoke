from secrets import token_urlsafe
from threading import Event
from time import monotonic, sleep

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.collection.service import TaskStore
from app.config import Settings
from app.main import _attach_existing_doubao_browser, create_app


def test_health_returns_ok(tmp_path) -> None:
    settings = Settings(
        app_env="test",
        database_url="sqlite+pysqlite:///:memory:",
        app_master_key=Fernet.generate_key().decode("ascii"),
        session_secret=token_urlsafe(32),
        data_dir=tmp_path / "data",
    )
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/v1/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


def test_collection_runtime_uses_json_store_without_postgres_recovery(tmp_path) -> None:
    settings = Settings(
        app_env="test",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'identity.db'}",
        app_master_key=Fernet.generate_key().decode("ascii"),
        session_secret=token_urlsafe(32),
        data_dir=tmp_path / "data",
    )
    app = create_app(settings)

    with TestClient(app) as client:
        assert client.get("/api/v1/health").json() == {"status": "ok"}
        assert isinstance(app.state.collection_service.store, TaskStore)
        assert not hasattr(app.state, "recovery_tasks")


def test_startup_attaches_to_an_already_running_doubao_browser() -> None:
    class RunningLauncher:
        def is_running(self) -> bool:
            return True

    class Gateway:
        def __init__(self) -> None:
            self.started = Event()

        def start(self):
            self.started.set()

    gateway = Gateway()

    _attach_existing_doubao_browser(gateway, RunningLauncher())

    assert gateway.started.wait(timeout=1)


def test_startup_attachment_does_not_block_on_slow_browser() -> None:
    class RunningLauncher:
        def is_running(self) -> bool:
            return True

    started = Event()
    release = Event()

    class SlowGateway:
        def start(self):
            started.set()
            release.wait(timeout=2)

    begin = monotonic()
    _attach_existing_doubao_browser(SlowGateway(), RunningLauncher())
    elapsed = monotonic() - begin

    assert elapsed < 0.5
    assert started.wait(timeout=0.5)
    release.set()
    sleep(0.01)


def test_startup_attachment_retries_when_browser_appears_after_backend_start() -> None:
    class FlakyLauncher:
        def __init__(self) -> None:
            self.calls = 0

        def is_running(self) -> bool:
            self.calls += 1
            return self.calls >= 2

    class Gateway:
        def __init__(self) -> None:
            self.started = Event()

        def start(self):
            self.started.set()

    gateway = Gateway()
    launcher = FlakyLauncher()

    _attach_existing_doubao_browser(gateway, launcher)

    assert gateway.started.wait(timeout=1)
