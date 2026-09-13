from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from echo_voc.errors import EchoError
from echo_voc.web.jobs import JobManager
from echo_voc.web.security import LocalGuardMiddleware, allowed_hosts
from echo_voc.web.templating import is_htmx, render

log = logging.getLogger(__name__)


def create_app(bind_host: str | None = "127.0.0.1") -> FastAPI:
    load_dotenv(Path.cwd() / ".env")
    jobs = JobManager()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        await jobs.shutdown()

    app = FastAPI(title="echo", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.jobs = jobs
    app.add_middleware(
        LocalGuardMiddleware,
        hosts=allowed_hosts(bind_host),
        password=os.getenv("ECHO_WEB_PASSWORD") or None,
    )
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

    @app.exception_handler(EchoError)
    @app.exception_handler(ValueError)
    async def user_error(request: Request, exc: Exception) -> HTMLResponse:
        if is_htmx(request):
            response = render(request, "partials/flash.html", level="error", message=str(exc))
            response.headers["HX-Retarget"] = "#flash"
            response.headers["HX-Reswap"] = "innerHTML"
            return response
        return render(request, "error.html", status_code=400, message=str(exc))

    from echo_voc.web.routes import analysis, insights, system, workspace

    for module in (system, workspace, analysis, insights):
        app.include_router(module.router)
    return app
