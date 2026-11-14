"""投资研究库的 Web 进程。默认只监听 127.0.0.1:8088。"""

from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from researchlib import embeddings
from researchlib.ask import mark_interrupted
from researchlib.chunks import relabel_ocr_chunks_if_needed
from researchlib.config import Settings, assert_bind_allowed
from researchlib.db import connect, prepare
from researchlib.duplicates import dedupe_if_needed
from researchlib.health import read_status, write_status
from researchlib.mentions import rebuild_if_stale
from researchlib.web.api import api
from researchlib.web.assets import IMMUTABLE, asset_version
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
        threading.Thread(
            target=_embedding_upkeep, args=(app.state.settings,), name="embedding-upkeep", daemon=True
        ).start()
    yield


def _embedding_upkeep(settings: Settings) -> None:
    """启动时做一次，之后每分钟看一次，两件事：
    - 本机 Ollama 闲置 5 分钟就卸下向量模型，之后的第一次搜索要等它重新加载几秒。隔一阵子没用过就算一个很短的
      向量（上线后的第一次搜索也不用等）。云端的向量服务不做（见 keep_warm）。
    - 检索页「向量还没算完」那句的块数隔五分钟重数一遍，好久没人搜以后第一个人看到的也是新数（见 keep_coverage_fresh）。
    """
    while True:
        try:
            embeddings.keep_warm()
        except Exception:  # 保温出错不影响网站
            logger.exception("向量服务保温出错")
        try:
            embeddings.keep_coverage_fresh(lambda: connect(settings))
        except Exception as exc:  # 数不出来就过几分钟再数，搜索时也会重数，不影响检索
            logger.info("定时重数向量块数没成：%s", exc)
        time.sleep(60)


def _background_upkeep(settings: Settings) -> None:
    """失败时写进状态文件（research-status 会列出来）；成功就清掉之前的失败。"""
    failed: list[str] = []
    try:
        dedupe_if_needed(settings)
    except Exception as exc:
        logger.exception("后台认重复文章失败，可以手动跑 research-dedupe")
        failed.append(f"认重复文章（{exc}），可以手动跑 research-dedupe")
    try:
        rebuild_if_stale(settings)
    except Exception as exc:
        logger.exception("后台重算公司提及失败，可以手动跑 research-rebuild-mentions")
        failed.append(f"重算公司提及（{exc}），可以手动跑 research-rebuild-mentions")
    try:
        relabel_ocr_chunks_if_needed(settings)
    except Exception as exc:
        logger.exception("后台重切图片文字块失败，可以手动跑 research-rebuild-chunks --ocr")
        failed.append(f"重切图片文字块（{exc}），可以手动跑 research-rebuild-chunks --ocr")
    previous = read_status(settings.data_dir, "upkeep")
    if failed:
        write_status(settings.data_dir, "upkeep", False, "；".join(failed))
    elif previous is not None and not previous.get("ok"):
        write_status(settings.data_dir, "upkeep", True)


def create_app(settings: Settings | None = None, *, experiment_owner_verifier=None) -> FastAPI:
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

    if app.state.settings.experiments_enabled:
        from researchlib.web.experiment_site import attach_experiment_admin

        attach_experiment_admin(app, app.state.settings, experiment_owner_verifier)

    # 地址里的 %00 解出来是 NUL 字符：Postgres 的文字和文件路径都不收，查下去只会 500（/search?q=%00、/media/%00）。
    # 正常链接不会带它，当成没有这一页。写在 privacy_headers 前面，这样回的 404 也带那几个响应头。
    @app.middleware("http")
    async def reject_nul(request: Request, call_next):
        if "\x00" in request.url.path or b"%00" in request.scope.get("query_string", b""):
            return await server_error(request, StarletteHTTPException(status_code=404))
        return await call_next(request)

    @app.middleware("http")
    async def privacy_headers(request: Request, call_next):
        started = time.perf_counter()
        response = await call_next(request)
        # 服务器上花了多久（不含网络）。浏览器开发者工具和 curl 都看得到，和总耗时一比就知道慢在哪一头。
        response.headers["Server-Timing"] = f"app;dur={(time.perf_counter() - started) * 1000:.1f}"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        # 网页上的图只从本站来（外部图片不外链）。正文过滤漏了的写法，浏览器这一层也不加载。
        is_page = "text/html" in response.headers.get("content-type", "")
        if is_page and "content-security-policy" not in response.headers:
            response.headers["Content-Security-Policy"] = "img-src 'self' data:"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        response.headers["Cache-Control"] = "private, no-store"
        # 样式和脚本带着当前内容的版本号来取（模板里的 asset()），一直缓存；见 web/assets.py。
        path = request.url.path
        if path.startswith("/static/") and response.status_code in (200, 304):
            wanted = request.query_params.get("v")
            if wanted and wanted == asset_version(path[len("/static/"):]):
                response.headers["Cache-Control"] = IMMUTABLE
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

    # 带类型的参数只有路径里的编号。编号不是数字（/articles/abc、/api/v1/answers/abc）就是没有这一页，
    # 和 /api/v1/articles/abc 一样回 404；FastAPI 默认回 422 和一串英文 JSON。
    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        return await server_error(request, StarletteHTTPException(status_code=404))

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
