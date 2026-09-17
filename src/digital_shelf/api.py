"""FastAPI entry point for the canonical application."""

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncEngine

from digital_shelf.config import Settings, get_settings
from digital_shelf.db import create_engine, database_ready
from digital_shelf.logging import configure_logging


def create_app(settings: Settings | None = None) -> FastAPI:
    runtime_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Any:
        configure_logging(runtime_settings.log_level)
        app.state.settings = runtime_settings
        app.state.engine = create_engine(runtime_settings.database_url.get_secret_value())
        yield
        await app.state.engine.dispose()

    application = FastAPI(
        title="Digital Shelf API",
        version="0.3.0",
        docs_url=None if runtime_settings.environment == "production" else "/docs",
        redoc_url=None,
        lifespan=lifespan,
    )

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

    return application


app = create_app()
