from contextlib import asynccontextmanager
from threading import Thread
from time import monotonic, sleep

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes.auth import router as auth_router
from app.api.routes.collection_tasks import router as collection_tasks_router
from app.api.routes.workspaces import router as workspaces_router
from app.collection.dedicated_chrome import DedicatedChromeLauncher, DedicatedChromeUnavailable
from app.collection.doubao_attention import DoubaoAttentionMonitor
from app.collection.doubao_browser import DoubaoWebChatGateway, DoubaoWebUnavailable
from app.collection.erp_delivery import ErpLeadDelivery
from app.collection.models import build_model_verifiers
from app.collection.providers import build_providers
from app.collection.service import CollectionService, TaskStore
from app.config import Settings, get_settings
from app.db import create_database_engine, create_session_factory
from app.models.base import Base


def _attach_existing_doubao_browser(
    gateway: DoubaoWebChatGateway | None,
    launcher: DedicatedChromeLauncher | None,
) -> None:
    """Reuse an existing CDP browser without blocking API startup.

    Chrome can take a moment to expose its CDP target during a backend restart.
    Keep the attachment asynchronous, but retry for a bounded window so tasks
    recovered at the same time do not race the browser startup.
    """
    if gateway is None or launcher is None:
        return

    def attach() -> None:
        deadline = monotonic() + 30
        while monotonic() < deadline:
            try:
                if launcher.is_running():
                    gateway.start()
                    return
            except (DedicatedChromeUnavailable, DoubaoWebUnavailable, TimeoutError):
                # CDP may still be coming up or may briefly disconnect. Keep the
                # bounded retry window alive; the explicit start/reconnect API
                # remains available after the window expires.
                pass
            except Exception:  # noqa: BLE001 - background attachment must not kill startup
                return
            sleep(0.25)

    Thread(target=attach, name="doubao-web-attach", daemon=True).start()


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or get_settings()
    engine = create_database_engine(resolved_settings)
    session_factory = create_session_factory(engine)
    store = TaskStore(resolved_settings.data_dir)
    providers = build_providers(resolved_settings)
    erp_delivery = ErpLeadDelivery(
        resolved_settings.data_dir,
        resolved_settings.erp_delivery_url
        if resolved_settings.erp_auto_push and resolved_settings.erp_delivery_url
        else None,
        timeout_seconds=resolved_settings.erp_delivery_timeout_seconds,
    )
    doubao_web_gateway = None
    doubao_web_launcher = None
    if resolved_settings.doubao_web_enabled:
        profile_dir = resolved_settings.doubao_web_profile_dir
        if not profile_dir.is_absolute():
            parts = profile_dir.parts
            relative_parts = parts[1:] if parts and parts[0].lower() == "data" else parts
            profile_dir = resolved_settings.data_dir.joinpath(*relative_parts)
        doubao_web_launcher = DedicatedChromeLauncher(
            profile_dir,
            resolved_settings.doubao_web_chat_url,
            cdp_host=resolved_settings.doubao_web_cdp_host,
            cdp_port=resolved_settings.doubao_web_cdp_port,
            executable=resolved_settings.doubao_web_browser_executable,
        )
        doubao_web_gateway = DoubaoWebChatGateway(
            cdp_url=doubao_web_launcher.cdp_url,
            chat_url=resolved_settings.doubao_web_chat_url,
            request_timeout_seconds=resolved_settings.doubao_web_request_timeout_seconds,
            discovery_timeout_seconds=resolved_settings.doubao_web_discovery_timeout_seconds,
            request_interval_min_seconds=resolved_settings.doubao_web_min_interval_seconds,
            request_interval_max_seconds=resolved_settings.doubao_web_max_interval_seconds,
            batch_size_min=resolved_settings.doubao_web_batch_min_size,
            batch_size_max=resolved_settings.doubao_web_batch_max_size,
            cooldown_min_seconds=resolved_settings.doubao_web_cooldown_min_seconds,
            cooldown_max_seconds=resolved_settings.doubao_web_cooldown_max_seconds,
            pacing_state_path=resolved_settings.data_dir / "doubao-web-pacing.json",
        )
    collection_service = CollectionService(
        store,
        providers,
        build_model_verifiers(resolved_settings),
        company_name_verifier=providers["web_search"],
        contact_verifier=providers["web_search"],
        lead_delivery=erp_delivery,
        doubao_web_gateway=doubao_web_gateway,
        max_concurrent_tasks=resolved_settings.collection_max_concurrent_tasks,
    )
    doubao_attention_monitor = None
    if doubao_web_gateway is not None:
        doubao_attention_monitor = DoubaoAttentionMonitor(
            waiting_task_ids=lambda: store.list_task_ids_by_status(["waiting_for_login"]),
            refresh_status=doubao_web_gateway.refresh_status,
            resume_task=collection_service.resume_task,
            poll_seconds=resolved_settings.doubao_web_attention_poll_seconds,
            ready_confirmations=resolved_settings.doubao_web_ready_confirmations,
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        Base.metadata.create_all(engine)
        _attach_existing_doubao_browser(doubao_web_gateway, doubao_web_launcher)
        if doubao_attention_monitor is not None:
            doubao_attention_monitor.start()
        try:
            yield
        finally:
            if doubao_attention_monitor is not None:
                doubao_attention_monitor.stop()
            collection_service.shutdown()
            engine.dispose()

    app = FastAPI(title="B2B Lead Platform", version="0.1.0", lifespan=lifespan)
    app.state.settings = resolved_settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.collection_service = collection_service
    app.state.erp_delivery = erp_delivery
    app.state.doubao_web_gateway = doubao_web_gateway
    app.state.doubao_web_launcher = doubao_web_launcher
    app.state.doubao_attention_monitor = doubao_attention_monitor
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/v1/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(auth_router)
    app.include_router(workspaces_router)
    app.include_router(collection_tasks_router)
    return app
