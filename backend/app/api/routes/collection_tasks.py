from collections.abc import Callable
from typing import Annotated, Literal

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response, status
from pydantic import AnyHttpUrl, BaseModel, Field, SecretStr, ValidationInfo, field_validator, model_validator

from app.api.deps import require_collection_access
from app.collection.cities import tier3_plus_city_names
from app.collection.dedicated_chrome import DedicatedChromeUnavailable
from app.collection.doubao_browser import DoubaoBrowserStatus, DoubaoWebTimeout, DoubaoWebUnavailable
from app.collection.exports import build_city_zip
from app.collection.intent import parse_collection_intent
from app.collection.models import ModelVerificationError
from app.collection.providers import ProviderError
from app.collection.service import CollectionService, InvalidTaskStateError, TaskNotFoundError

router = APIRouter(
    prefix="/api/v1",
    tags=["collection"],
    dependencies=[Depends(require_collection_access)],
)
KNOWN_PROVIDERS = {"web_search"}
KNOWN_MODELS = {"deepseek", "doubao"}


class CreateCollectionTaskRequest(BaseModel):
    name: str = Field(min_length=1)
    request_text: str = Field(min_length=1)
    cities: list[str] = Field(default_factory=list)
    all_cities: bool = False
    keywords: list[str] = Field(min_length=1)
    providers: list[str] = Field(min_length=1)
    models: list[str] = Field(min_length=1)
    required_contacts: list[Literal["mobile", "email", "wechat"]] = Field(min_length=1)
    auto_deliver: bool = False
    continuous_mode: bool = False
    verified_auto_deliver: bool = False
    use_doubao_web_chat: bool = False
    web_chat_discovery: bool = False

    @field_validator("cities", "keywords")
    @classmethod
    def clean_values(cls, values: list[str], info: ValidationInfo) -> list[str]:
        cleaned = list(dict.fromkeys(value.strip() for value in values if value.strip()))
        if not cleaned and info.field_name == "keywords":
            raise ValueError("at least one non-empty value is required")
        return cleaned

    @field_validator("providers")
    @classmethod
    def validate_providers(cls, values: list[str]) -> list[str]:
        cleaned = list(dict.fromkeys(value.strip().lower() for value in values))
        if not cleaned or not set(cleaned) <= KNOWN_PROVIDERS:
            raise ValueError("providers contain an unknown real data source")
        return cleaned

    @field_validator("models")
    @classmethod
    def validate_models(cls, values: list[str]) -> list[str]:
        cleaned = list(dict.fromkeys(value.strip().lower() for value in values if value.strip()))
        if not set(cleaned) <= KNOWN_MODELS:
            raise ValueError("models contain an unknown verification model")
        return cleaned

    @field_validator("required_contacts")
    @classmethod
    def validate_required_contacts(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(values))

    @model_validator(mode="after")
    def validate_city_scope(self) -> "CreateCollectionTaskRequest":
        if not self.all_cities and not self.cities:
            raise ValueError("cities are required unless all_cities is true")
        if self.auto_deliver and self.verified_auto_deliver:
            raise ValueError("automatic delivery modes are mutually exclusive")
        return self


class DeliveryRequest(BaseModel):
    target_url: AnyHttpUrl
    bearer_token: SecretStr | None = None
    batch_size: int = Field(default=100, ge=1, le=500)


class SingleLeadDeliveryRequest(BaseModel):
    city: str = Field(min_length=1)
    company_name: str = Field(min_length=1)
    target_url: AnyHttpUrl
    bearer_token: SecretStr | None = None


class NaturalLanguageCollectionTaskRequest(BaseModel):
    request_text: str = Field(min_length=1, max_length=4000)
    models: list[str] = Field(min_length=1)
    auto_deliver: bool = False
    continuous_mode: bool = False
    verified_auto_deliver: bool = False
    use_doubao_web_chat: bool = False
    web_chat_discovery: bool = False

    @field_validator("models")
    @classmethod
    def validate_models(cls, values: list[str]) -> list[str]:
        cleaned = list(dict.fromkeys(value.strip().lower() for value in values if value.strip()))
        if not cleaned or not set(cleaned) <= KNOWN_MODELS:
            raise ValueError("models contain an unknown verification model")
        return cleaned

    @model_validator(mode="after")
    def validate_delivery_mode(self) -> "NaturalLanguageCollectionTaskRequest":
        if self.auto_deliver and self.verified_auto_deliver:
            raise ValueError("automatic delivery modes are mutually exclusive")
        return self


def _service(request: Request) -> CollectionService:
    return request.app.state.collection_service


def _owner(request: Request) -> str:
    return request.state.collection_owner


def _doubao_web_response(gateway_status: DoubaoBrowserStatus, *, enabled: bool = True) -> dict:
    return {
        "enabled": enabled,
        "state": gateway_status.state,
        "detail": gateway_status.detail,
        "current_company": gateway_status.current_company,
    }


def _claim_legacy_local_tasks(
    service: CollectionService,
    owner: str,
    legacy_owner_id: str | None,
) -> None:
    if not legacy_owner_id or owner != legacy_owner_id:
        return
    for task in service.store.list_tasks():
        if task.get("created_by") != "local-user":
            continue

        def claim(current: dict) -> None:
            if current.get("created_by") == "local-user":
                current["created_by"] = owner

        service.store.update(task["id"], claim)


def _load(service: CollectionService, task_id: str, request: Request) -> dict:
    try:
        task = service.get_task(task_id)
    except TaskNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="collection task not found") from exc
    if task.get("created_by") != _owner(request):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="collection task not found")
    return task


def _load_metadata(service: CollectionService, task_id: str, request: Request) -> dict:
    try:
        task = service.get_task_metadata(task_id)
    except TaskNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="collection task not found") from exc
    if task.get("created_by") != _owner(request):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="collection task not found")
    return task


def _control_response(action: Callable[[], dict]) -> dict:
    try:
        return CollectionService.task_summary(action())
    except TaskNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="collection task not found") from exc
    except InvalidTaskStateError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc


@router.get("/providers")
def providers(request: Request) -> dict:
    return {"items": _service(request).provider_items()}


@router.get("/models")
def models(request: Request) -> dict:
    return {"items": _service(request).model_items()}


@router.get("/delivery-config")
def delivery_config(request: Request) -> dict[str, bool]:
    return {"configured": request.app.state.erp_delivery.configured}


@router.get("/collection-tasks/doubao-web/status")
def doubao_web_status(request: Request) -> dict:
    gateway = _service(request).doubao_web_gateway
    launcher = request.app.state.doubao_web_launcher
    if gateway is None or launcher is None:
        return {
            "enabled": False,
            "state": "disabled",
            "detail": "豆包网页采集未启用",
            "current_company": None,
        }
    if not launcher.is_running():
        return _doubao_web_response(
            DoubaoBrowserStatus("browser_stopped", "豆包专用浏览器未启动", None)
        )
    try:
        return _doubao_web_response(gateway.status())
    except (DoubaoWebTimeout, TimeoutError):
        # Status polling must not turn a slow browser operation into a page-level 500.
        return _doubao_web_response(
            DoubaoBrowserStatus("timeout", "豆包网页状态查询超时，任务继续运行", None)
        )
    except DoubaoWebUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@router.post("/collection-tasks/doubao-web/start")
def start_doubao_web(request: Request) -> dict:
    gateway = _service(request).doubao_web_gateway
    launcher = request.app.state.doubao_web_launcher
    if gateway is None or launcher is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="豆包网页采集未启用",
        )
    try:
        launcher.start()
        return _doubao_web_response(gateway.start())
    except (DedicatedChromeUnavailable, DoubaoWebUnavailable, DoubaoWebTimeout) as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@router.post("/collection-tasks/doubao-web/reconnect")
def reconnect_doubao_web(request: Request) -> dict:
    gateway = _service(request).doubao_web_gateway
    launcher = request.app.state.doubao_web_launcher
    if gateway is None or launcher is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="豆包网页采集未启用",
        )
    if not launcher.is_running():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="豆包专用浏览器未启动",
        )
    try:
        return _doubao_web_response(gateway.reconnect())
    except DoubaoWebUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@router.post("/collection-tasks", status_code=status.HTTP_201_CREATED)
def create_collection_task(payload: CreateCollectionTaskRequest, request: Request) -> dict:
    service = _service(request)
    available_providers = {item["id"] for item in service.provider_items() if item["configured"]}
    available_models = {item["id"] for item in service.model_items() if item["configured"]}
    if not set(payload.providers) <= available_providers:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="selected source is not configured")
    if not set(payload.models) <= available_models:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="selected model is not configured")
    if (
        payload.auto_deliver or payload.verified_auto_deliver
    ) and not request.app.state.erp_delivery.configured:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="ERP automatic delivery is not configured",
        )
    task_payload = payload.model_dump()
    if payload.all_cities:
        try:
            task_payload["cities"] = service.city_names()
        except ProviderError as exc:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    return service.task_summary(service.create_task(task_payload, created_by=_owner(request)))


@router.post("/collection-tasks/from-natural-language", status_code=status.HTTP_201_CREATED)
def create_natural_language_collection_task(
    payload: NaturalLanguageCollectionTaskRequest,
    request: Request,
) -> dict:
    service = _service(request)
    configured_models = {item["id"] for item in service.model_items() if item["configured"]}
    if not set(payload.models) <= configured_models:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="selected model is not configured",
        )
    if (
        payload.auto_deliver or payload.verified_auto_deliver
    ) and not request.app.state.erp_delivery.configured:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="ERP automatic delivery is not configured",
        )
    model_keywords: list[str] = []
    for model_id in payload.models:
        parser = service.verifiers.get(model_id)
        parse_keywords = getattr(parser, "parse_intent_keywords", None)
        if callable(parse_keywords):
            try:
                model_keywords.extend(parse_keywords(payload.request_text))
            except ModelVerificationError:
                continue
    intent = parse_collection_intent(
        payload.request_text,
        configured_models=set(payload.models),
        model_keywords=list(dict.fromkeys(model_keywords)),
    )
    if "web_search" not in {item["id"] for item in service.provider_items() if item["configured"]}:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="public web search is unavailable",
        )
    cities = tier3_plus_city_names() if intent.all_cities else intent.cities
    task_payload = {
        "name": payload.request_text[:40],
        "request_text": payload.request_text,
        "cities": cities,
        "all_cities": intent.all_cities,
        "keywords": intent.keywords,
        "providers": ["web_search"],
        "models": payload.models,
        "required_contacts": intent.required_contacts,
        "contact_match": intent.contact_match,
        "limit_per_city": intent.limit_per_city,
        "auto_deliver": payload.auto_deliver,
        "continuous_mode": payload.continuous_mode,
        "verified_auto_deliver": payload.verified_auto_deliver,
        "use_doubao_web_chat": payload.use_doubao_web_chat,
        "web_chat_discovery": payload.web_chat_discovery,
    }
    return service.task_summary(service.create_task(task_payload, created_by=_owner(request)))


@router.get("/collection-tasks")
def list_collection_tasks(request: Request) -> dict:
    service = _service(request)
    owner = _owner(request)
    _claim_legacy_local_tasks(
        service,
        owner,
        request.app.state.settings.legacy_collection_owner_id,
    )
    return {
        "items": [
            service.task_summary(task)
            for task in service.store.list_tasks()
            if task.get("created_by") == owner
        ]
    }


@router.post("/collection-tasks/{task_id}/start", status_code=status.HTTP_202_ACCEPTED)
def start_collection_task(task_id: str, request: Request) -> dict:
    service = _service(request)
    _load_metadata(service, task_id, request)
    return _control_response(lambda: service.start_task(task_id))


@router.post("/collection-tasks/{task_id}/pause", status_code=status.HTTP_202_ACCEPTED)
def pause_collection_task(task_id: str, request: Request) -> dict:
    service = _service(request)
    _load_metadata(service, task_id, request)
    return _control_response(lambda: service.pause_task(task_id))


@router.post("/collection-tasks/{task_id}/resume", status_code=status.HTTP_202_ACCEPTED)
def resume_collection_task(task_id: str, request: Request) -> dict:
    service = _service(request)
    _load_metadata(service, task_id, request)
    return _control_response(lambda: service.resume_task(task_id))


@router.post("/collection-tasks/{task_id}/terminate", status_code=status.HTTP_202_ACCEPTED)
def terminate_collection_task(task_id: str, request: Request) -> dict:
    service = _service(request)
    _load_metadata(service, task_id, request)
    return _control_response(lambda: service.terminate_task(task_id))


@router.get("/collection-tasks/{task_id}")
def get_collection_task(task_id: str, request: Request) -> dict:
    service = _service(request)
    return service.task_summary(_load(service, task_id, request))


@router.get("/collection-tasks/{task_id}/delivery-status")
def get_collection_delivery_status(task_id: str, request: Request) -> dict:
    service = _service(request)
    _load_metadata(service, task_id, request)
    delivery = request.app.state.erp_delivery
    return {
        "configured": delivery.configured,
        **delivery.failure_counts(task_id),
    }


@router.post("/collection-tasks/{task_id}/retry-delivery")
def retry_collection_delivery(task_id: str, request: Request) -> dict:
    service = _service(request)
    _load_metadata(service, task_id, request)
    return service.retry_delivery(task_id)


@router.post("/collection-tasks/{task_id}/retry-errors", status_code=status.HTTP_202_ACCEPTED)
def retry_collection_errors(task_id: str, request: Request) -> dict:
    service = _service(request)
    _load_metadata(service, task_id, request)
    return _control_response(lambda: service.retry_errors(task_id))


@router.post(
    "/collection-tasks/{task_id}/enrich-contacts",
    status_code=status.HTTP_202_ACCEPTED,
)
def enrich_collection_contacts(
    task_id: str,
    request: Request,
) -> dict:
    service = _service(request)
    _load_metadata(service, task_id, request)
    return _control_response(lambda: service.enrich_contacts(task_id))


@router.post("/collection-tasks/{task_id}/verify", status_code=status.HTTP_202_ACCEPTED)
def verify_collection_task(task_id: str, request: Request, background_tasks: BackgroundTasks) -> dict:
    service = _service(request)
    try:
        _load_metadata(service, task_id, request)
        task, should_run = service.prepare_verification(task_id)
    except TaskNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="collection task not found") from exc
    if should_run:
        background_tasks.add_task(service.verify_task, task_id)
    return service.task_summary(task)


@router.get("/collection-tasks/{task_id}/leads")
def get_collection_leads(
    task_id: str,
    request: Request,
    city: Annotated[str | None, Query()] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> dict:
    service = _service(request)
    _load_metadata(service, task_id, request)
    items = service.store.list_leads(task_id, city=city, offset=offset, limit=limit)
    total = service.store.count_leads(task_id, city=city)
    return {"items": items, "total": total, "offset": offset, "limit": limit}


@router.get("/collection-tasks/{task_id}/export")
def export_collection_task(
    task_id: str,
    request: Request,
    format: Annotated[Literal["xlsx", "csv", "json"], Query()] = "xlsx",
) -> Response:
    service = _service(request)
    task = _load_metadata(service, task_id, request)
    content = build_city_zip(
        task,
        format,
        service.store.iter_leads(task_id, batch_size=500),
    )
    (service.store.exports_dir / f"{task_id}-{format}.zip").write_bytes(content)
    return Response(
        content=content,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{task_id}-{format}.zip"'},
    )


@router.post("/collection-tasks/{task_id}/deliver")
def deliver_collection_task(task_id: str, payload: DeliveryRequest, request: Request) -> dict:
    service = _service(request)
    _load_metadata(service, task_id, request)
    try:
        return service.deliver_task(
            task_id,
            target_url=str(payload.target_url),
            bearer_token=payload.bearer_token.get_secret_value() if payload.bearer_token else None,
            max_items=100,
            client=getattr(request.app.state, "delivery_client", None),
        )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="target system rejected delivery") from exc


@router.post("/collection-tasks/{task_id}/leads/deliver")
def deliver_single_collection_lead(
    task_id: str,
    payload: SingleLeadDeliveryRequest,
    request: Request,
) -> dict:
    service = _service(request)
    _load_metadata(service, task_id, request)
    try:
        return service.deliver_lead(
            task_id,
            city=payload.city,
            company_name=payload.company_name,
            target_url=str(payload.target_url),
            bearer_token=payload.bearer_token.get_secret_value() if payload.bearer_token else None,
            client=getattr(request.app.state, "delivery_client", None),
        )
    except TaskNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="collection task not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
