"""投资研究库的 Web 进程。默认只监听 127.0.0.1:8088。"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from researchlib.config import Settings, assert_bind_allowed
from researchlib.db import prepare
from researchlib.web.routes import TEMPLATES, render_page, router

STATIC_DIR = str(Path(__file__).resolve().parent / "static")
logger = logging.getLogger("researchlib")


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = prepare(app.state.settings)
    conn.close()
    yield


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(
        title="投资研究库",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings or Settings.from_env()
    app.state.sources_mtime = None
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(router)

    @app.middleware("http")
    async def privacy_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        response.headers["Cache-Control"] = "private, no-store"
        return response

    @app.exception_handler(Exception)
    async def server_error(request: Request, exc: Exception):
        if isinstance(exc, StarletteHTTPException):
            if exc.status_code == 404:
                return render_page(request, "404.html", {"title": "没有这一页"}, status_code=404)
            detail = exc.detail if isinstance(exc.detail, str) else ""
            return PlainTextResponse(detail, status_code=exc.status_code)
        logger.exception("页面处理失败")
        return TEMPLATES.TemplateResponse(
            request,
            "404.html",
            {
                "request": request,
                "title": "页面出错",
                "nav": "",
                "query": "",
                "public_host": request.app.state.settings.public_host,
                "canonical": request.app.state.settings.public_base_url + request.url.path,
                "sample_banner": False,
                "sources": [],
                "active_source": "",
                "message": "页面出错，请看服务日志。",
            },
            status_code=500,
        )

    return app


def main() -> None:
    import uvicorn

    settings = Settings.from_env()
    assert_bind_allowed(settings.bind_host, settings.allow_nonlocal_bind)
    uvicorn.run(
        create_app(settings),
        host=settings.bind_host,
        port=settings.bind_port,
        log_level="info",
        access_log=False,
    )


if __name__ == "__main__":
    main()
