"""投资研究库的 Web 进程。默认只监听 127.0.0.1:8088。"""

from __future__ import annotations

import logging
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from researchlib.ask import mark_interrupted
from researchlib.config import Settings, assert_bind_allowed
from researchlib.db import prepare
from researchlib.duplicates import dedupe_if_needed
from researchlib.mentions import rebuild_if_stale
from researchlib.web.api import api
from researchlib.web.mcp import mcp
from researchlib.web.routes import TEMPLATES, render_page, router

STATIC_DIR = str(Path(__file__).resolve().parent / "static")
logger = logging.getLogger("researchlib")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 迁移、白名单、公司种子，并给缺块的文章补切。不在启动时跑 research-digest。
    conn = prepare(app.state.settings)
    try:
        # 上次没跑完的问库整合标成出错，回答页可以重新整合。
        interrupted = mark_interrupted(conn)
        if interrupted:
            logger.warning("有 %s 条问库整合因重启中断", interrupted)
    finally:
        conn.close()
    # 后台顺序做两件事，不挡启动：先认重复文章（第一次部署时全库认一遍），再在公司别名或规则变了时重算提及。
    # 放在同一个线程里依次跑，免得重算提及时把刚认出的重复又记上提及。测试进程里不跑。
    if "PYTEST_CURRENT_TEST" not in os.environ:
        threading.Thread(
            target=_background_upkeep, args=(app.state.settings,), name="library-upkeep", daemon=True
        ).start()
    yield


def _background_upkeep(settings: Settings) -> None:
    try:
        dedupe_if_needed(settings)
    except Exception:
        logger.exception("后台认重复文章失败，可以手动跑 research-dedupe")
    try:
        rebuild_if_stale(settings)
    except Exception:
        logger.exception("后台重算公司提及失败，可以手动跑 research-rebuild-mentions")


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
    app.include_router(api)
    app.include_router(mcp)
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

    # 同时接管 HTTPException，否则 FastAPI 默认把 404 回成 {"detail": "Not Found"}，404 页面从来不出现。
    @app.exception_handler(StarletteHTTPException)
    @app.exception_handler(Exception)
    async def server_error(request: Request, exc: Exception):
        if request.url.path.startswith("/api/"):
            status = exc.status_code if isinstance(exc, StarletteHTTPException) else 500
            if status == 500:
                logger.exception("接口处理失败")
            detail = exc.detail if isinstance(exc, StarletteHTTPException) and isinstance(exc.detail, str) else ""
            return JSONResponse({"error": {"status": status, "message": detail or "出错了"}}, status_code=status)
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
