"""FastAPI entry point for the canonical application."""

from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncEngine

from digital_shelf.admin import (
    AdminAuthorizer,
    AdminPrincipal,
    AuthorizationError,
    DatabaseAdminAuthorizer,
    DatabaseFeatureStore,
    FeatureNotFoundError,
    FeatureStore,
    FeatureUpdateCommand,
    FeatureView,
    RevisionConflictError,
)
from digital_shelf.admin_audit import AuditEventView, AuditStore, DatabaseAuditStore
from digital_shelf.auth import (
    AuthenticationError,
    SupabaseJWTVerifier,
    TokenVerifier,
    parse_bearer_token,
)
from digital_shelf.config import Settings, get_settings
from digital_shelf.db import create_engine, database_ready
from digital_shelf.features import FeatureKey
from digital_shelf.logging import configure_logging
from digital_shelf.store_settings import (
    DatabaseSettingStore,
    InvalidSettingValueError,
    SettingRevisionConflictError,
    SettingsPatchCommand,
    SettingStore,
    SettingView,
    validate_settings_command,
)


def create_app(
    settings: Settings | None = None,
    *,
    jwt_verifier: TokenVerifier | None = None,
    admin_authorizer: AdminAuthorizer | None = None,
    feature_store: FeatureStore | None = None,
    setting_store: SettingStore | None = None,
    audit_store: AuditStore | None = None,
) -> FastAPI:
    runtime_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Any:
        configure_logging(runtime_settings.log_level)
        app.state.settings = runtime_settings
        app.state.engine = create_engine(runtime_settings.database_url.get_secret_value())
        app.state.jwt_verifier = jwt_verifier
        if app.state.jwt_verifier is None and runtime_settings.supabase_auth_issuer is not None:
            app.state.jwt_verifier = SupabaseJWTVerifier(
                issuer=runtime_settings.supabase_auth_issuer,
                audience=runtime_settings.admin_jwt_audience,
            )
        app.state.admin_authorizer = admin_authorizer or DatabaseAdminAuthorizer(app.state.engine)
        app.state.feature_store = feature_store or DatabaseFeatureStore(app.state.engine)
        app.state.setting_store = setting_store or DatabaseSettingStore(app.state.engine)
        app.state.audit_store = audit_store or DatabaseAuditStore(app.state.engine)
        yield
        await app.state.engine.dispose()

    application = FastAPI(
        title="Digital Shelf API",
        version="0.3.0",
        docs_url=None if runtime_settings.environment == "production" else "/docs",
        redoc_url=None,
        lifespan=lifespan,
    )

    @application.middleware("http")
    async def correlation_id(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request.state.correlation_id = uuid4()
        response = await call_next(request)
        response.headers["X-Correlation-ID"] = str(request.state.correlation_id)
        return response

    async def current_admin(
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
    ) -> AdminPrincipal:
        verifier: TokenVerifier | None = request.app.state.jwt_verifier
        if verifier is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "admin authentication unavailable")
        try:
            token = parse_bearer_token(authorization)
            identity = await verifier.verify(token)
            authorizer: AdminAuthorizer = request.app.state.admin_authorizer
            return await authorizer.authorize(identity)
        except AuthenticationError as exc:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "authentication required",
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc
        except AuthorizationError as exc:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "permission denied") from exc

    def require_permission(admin: AdminPrincipal, permission: str) -> None:
        if not admin.allows(permission):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "permission denied")

    @application.get("/live", tags=["health"])
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/ready", tags=["health"])
    async def ready(request: Request, response: Response) -> dict[str, str]:
        engine: AsyncEngine = request.app.state.engine
        if not await database_ready(engine):
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {"status": "unavailable"}
        return {"status": "ready"}

    @application.get("/admin/v1/features", response_model=list[FeatureView], tags=["admin"])
    async def list_features(
        request: Request,
        admin: Annotated[AdminPrincipal, Depends(current_admin)],
    ) -> list[FeatureView]:
        require_permission(admin, "features.read")
        store: FeatureStore = request.app.state.feature_store
        return list(await store.list_features())

    @application.patch(
        "/admin/v1/features/{feature_key}",
        response_model=FeatureView,
        tags=["admin"],
    )
    async def update_feature(
        feature_key: str,
        command: FeatureUpdateCommand,
        request: Request,
        admin: Annotated[AdminPrincipal, Depends(current_admin)],
    ) -> FeatureView:
        require_permission(admin, "features.manage")
        try:
            feature = FeatureKey(feature_key)
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "unknown feature key") from exc
        store: FeatureStore = request.app.state.feature_store
        correlation = request.state.correlation_id
        if not isinstance(correlation, UUID):
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "correlation unavailable")
        try:
            return await store.update_feature(
                feature=feature,
                command=command,
                actor_admin_id=admin.admin_id,
                correlation_id=correlation,
            )
        except FeatureNotFoundError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "unknown feature key") from exc
        except RevisionConflictError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, "feature changed; reload and retry") from exc

    @application.get("/admin/v1/settings", response_model=list[SettingView], tags=["admin"])
    async def list_settings(
        request: Request,
        admin: Annotated[AdminPrincipal, Depends(current_admin)],
    ) -> list[SettingView]:
        require_permission(admin, "settings.read")
        store: SettingStore = request.app.state.setting_store
        return list(await store.list_settings())

    @application.patch("/admin/v1/settings", response_model=list[SettingView], tags=["admin"])
    async def update_settings(
        command: SettingsPatchCommand,
        request: Request,
        admin: Annotated[AdminPrincipal, Depends(current_admin)],
    ) -> list[SettingView]:
        require_permission(admin, "settings.manage")
        try:
            validate_settings_command(command)
        except KeyError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "unknown setting key") from exc
        except InvalidSettingValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid setting value") from exc

        store: SettingStore = request.app.state.setting_store
        try:
            return list(
                await store.update_settings(
                    command=command,
                    actor_admin_id=admin.admin_id,
                    correlation_id=request.state.correlation_id,
                )
            )
        except SettingRevisionConflictError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, "setting changed; reload and retry") from exc

    @application.get("/admin/v1/audit", response_model=list[AuditEventView], tags=["admin"])
    async def list_audit_events(
        request: Request,
        admin: Annotated[AdminPrincipal, Depends(current_admin)],
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> list[AuditEventView]:
        require_permission(admin, "audit.read")
        store: AuditStore = request.app.state.audit_store
        return list(await store.list_events(limit=limit))

    return application


app = create_app()
