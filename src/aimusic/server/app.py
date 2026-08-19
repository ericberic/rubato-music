"""FastAPI application entry point."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import TypeAdapter

from aimusic.core import paths
from aimusic.core.events import events
from aimusic.server.live_runtime import live_runtime
from aimusic.server.routes import router
from aimusic.server.schemas import TakeEvent
from aimusic.takes.aligner import alignment_worker
from aimusic.takes.materializer import materialization_worker
from aimusic.takes.review import review_worker


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    # The alignment worker publishes from a background thread (design doc
    # §2.7); it needs the loop this app is actually running on to hand
    # events to WS clients via call_soon_threadsafe.
    events.bind_loop(asyncio.get_running_loop())
    alignment_worker.recover_all()
    materialization_worker.recover_all()
    review_worker.recover_all()
    try:
        yield
    finally:
        live_runtime.shutdown()
        events.shutdown()


def _inject_websocket_event_schemas(schema: dict[str, Any]) -> dict[str, Any]:
    """Add the `WS /api/events` payload union (`TakeEvent`) to the OpenAPI
    document's `components.schemas`.

    FastAPI only auto-includes schemas reachable from an HTTP request/
    response `response_model`; `/api/events` is a plain `WebSocket` route
    (design doc §2.1), so its payload union is otherwise invisible to the
    spec -- and therefore to issue #83/#84's future generated TS client.
    """

    adapter = TypeAdapter(TakeEvent)
    event_schema = adapter.json_schema(ref_template="#/components/schemas/{model}")
    components = schema.setdefault("components", {})
    schemas = components.setdefault("schemas", {})
    schemas.update(event_schema.pop("$defs", {}))
    schemas["TakeEvent"] = event_schema
    return schema


def create_app() -> FastAPI:
    app = FastAPI(title="Rubato Server", version="0.1.0", lifespan=_lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)

    static_dir = paths.project_root() / "src" / "aimusic" / "server" / "static"
    if static_dir.exists():
        app.mount("/app", StaticFiles(directory=static_dir, html=True), name="ui")

        @app.get("/", include_in_schema=False)
        def root_redirect() -> RedirectResponse:  # type: ignore[unused-ignore]
            return RedirectResponse(url="/app/", status_code=307)

    default_openapi = app.openapi

    def _openapi_with_ws_events() -> dict[str, Any]:
        return _inject_websocket_event_schemas(default_openapi())

    app.openapi = _openapi_with_ws_events  # type: ignore[method-assign]

    return app


def _cors_origins() -> list[str]:
    env_value = os.environ.get("AIMUSIC_CORS_ORIGINS")
    if env_value:
        return [origin.strip() for origin in env_value.split(",") if origin.strip()]
    return [
        "http://localhost",
        "http://localhost:8000",
        "http://localhost:5173",
        "http://127.0.0.1",
        "http://127.0.0.1:8000",
        "http://127.0.0.1:5173",
    ]


app = create_app()


def main() -> None:  # pragma: no cover
    # Built as an explicit Config/Server pair (rather than the uvicorn.run()
    # convenience wrapper) so the running server instance can be stashed on
    # app.state -- the shutdown route (routes.py) needs a handle to flip
    # `should_exit` from inside a request, and there's no other way to reach
    # it from a plain `uvicorn.run("module:app", ...)` call (rubato#100).
    config = uvicorn.Config(app, host="0.0.0.0", port=8000, reload=False)
    server = uvicorn.Server(config)
    app.state.uvicorn_server = server
    server.run()


if __name__ == "__main__":  # pragma: no cover
    main()
