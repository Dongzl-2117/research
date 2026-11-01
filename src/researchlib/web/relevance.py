"""独立标注服务的路由；不挂入公开站点，不访问业务数据库。"""

from __future__ import annotations

import ipaddress
import secrets
from pathlib import Path
from typing import Literal, Protocol

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates

from researchlib.relevance.schema import GenerateRequest, LabelRequest, LabelSource, Task, canonical, normalized_query
from researchlib.relevance.store import Conflict, Record, Store, validate_records

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
FORWARDED_HEADERS = {
    "forwarded",
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-proto",
    "x-real-ip",
    "cf-connecting-ip",
    "cf-ray",
    "cdn-loop",
    "true-client-ip",
}
router = APIRouter()


class CandidateProvider(Protocol):
    def __call__(self, *, query: str, scope: dict) -> Task:
        """显式获取实际库的两段原文；返回含版本/分组信息的 Task，不生成答案。"""
        ...


def create_annotation_app(db_path: Path, annotator_id: str, *, candidate_provider: CandidateProvider | None = None):
    """仅本机专用进程。标注身份来自操作员配置，不能由访客在请求中指定。"""
    source = LabelSource(kind="human", labeler_id=annotator_id)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.relevance_store = Store(db_path)
    app.state.relevance_source = source
    app.state.relevance_provider = candidate_provider
    app.state.relevance_csrf = secrets.token_urlsafe(32)

    @app.middleware("http")
    async def local_access(request: Request, call_next):
        try:
            peer_local = bool(request.client and ipaddress.ip_address(request.client.host).is_loopback)
        except ValueError:
            peer_local = False
        host_local = request.url.hostname in {"127.0.0.1", "localhost", "::1"}
        origin = request.headers.get("origin")
        same_origin = not origin or origin == str(request.base_url).rstrip("/")
        if (
            not peer_local
            or not host_local
            or FORWARDED_HEADERS.intersection(request.headers)
            or not same_origin
            or request.headers.get("sec-fetch-site") == "cross-site"
        ):
            response = JSONResponse({"detail": "标注入口只允许直接连接本机，拒绝代理/跨站请求"}, status_code=403)
        elif request.method != "GET" and not secrets.compare_digest(
            request.headers.get("x-relevance-csrf", ""), app.state.relevance_csrf
        ):
            response = JSONResponse({"detail": "缺少本机标注页面的 CSRF 凭据"}, status_code=403)
        else:
            response = await call_next(request)
        response.headers.update(
            {
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "X-Frame-Options": "DENY",
                "X-Robots-Tag": "noindex, nofollow",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            }
        )
        return response

    @app.exception_handler(Conflict)
    async def conflict(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(LookupError)
    async def missing(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    app.include_router(router)
    return app


def view(store, dataset, frozen, source):
    presentation = store.present(frozen.task.task_id, source.labeler_id)
    current = dataset.current(frozen.task.task_id, source)
    by_id = {c.candidate_id: c.model_dump(mode="json") for c in frozen.task.candidates}
    return {
        "task_id": frozen.task.task_id,
        "query": frozen.task.query,
        "scope": frozen.task.scope,
        "material": frozen.task.material,
        "split": frozen.split,
        "presentation_id": presentation.presentation_id,
        "left": by_id[presentation.left_id],
        "right": by_id[presentation.right_id],
        "revision": current.revision if current else 0,
        "decision": current.decision if current else None,
    }


@router.get("/annotate")
def annotation_page(request: Request):
    return TEMPLATES.TemplateResponse(
        request,
        "relevance.html",
        {
            "csrf": request.app.state.relevance_csrf,
            "annotator": request.app.state.relevance_source.labeler_id,
            "provider_enabled": request.app.state.relevance_provider is not None,
        },
    )


@router.get("/annotate/app.js")
def annotation_script():
    return FileResponse(Path(__file__).resolve().parent / "static" / "relevance.js", media_type="text/javascript")


@router.get("/api/relevance/state")
def annotation_state(request: Request):
    store, source = request.app.state.relevance_store, request.app.state.relevance_source
    dataset = store.snapshot()
    active = [(f, dataset.current(f.task.task_id, source)) for f in dataset.tasks]
    todo = next((f for f, event in active if event is None or event.decision == "withdraw"), None)
    labelled = [(f, e) for f, e in active if e and e.decision != "withdraw"]
    previous = max(labelled, key=lambda pair: pair[1].created_at)[0] if labelled else None
    return {
        "total": len(active),
        "completed": len(labelled),
        "coverage": dataset.report(),
        "current": view(store, dataset, todo, source) if todo else None,
        "previous": view(store, dataset, previous, source) if previous else None,
    }


@router.post("/api/relevance/labels")
def label(request: Request, body: LabelRequest):
    return request.app.state.relevance_store.label(body, request.app.state.relevance_source)


@router.post("/api/relevance/generate")
def generate(request: Request, body: GenerateRequest):
    provider = request.app.state.relevance_provider
    if provider is None:
        raise HTTPException(503, "尚未配置实际库候选 provider；演示任务不会冒充查询检索结果")
    task = Task.model_validate(provider(query=normalized_query(body.query), scope=body.scope))
    if task.query != normalized_query(body.query) or task.scope != body.scope or task.material != "private_corpus":
        raise ValueError("provider 必须返回本次 query/scope 的实际原文任务")
    frozen = request.app.state.relevance_store.add_tasks([task])
    result = next(item for item in frozen if item.task.task_id == task.task_id)
    return {"task_id": task.task_id, "split": result.split, "group_id": result.group_id}


@router.get("/api/relevance/export")
def export(request: Request, source: Literal["human", "ai_weak", "all"] = "human"):
    content = request.app.state.relevance_store.export(source)
    dataset = validate_records([Record.model_validate_json(line) for line in content.splitlines()])
    return Response(
        content,
        media_type="application/x-ndjson",
        headers={
            "Content-Disposition": 'attachment; filename="relevance-private.jsonl"',
            "X-Relevance-Split-Warnings": canonical([w["code"] for w in dataset.report()["warnings"]]),
            "Link": '</api/relevance/report>; rel="describedby"',
        },
    )


@router.get("/api/relevance/report")
def report(request: Request):
    return request.app.state.relevance_store.snapshot().report()


@router.get("/annotate/source/{candidate_id}")
def synthetic_source(request: Request, candidate_id: str):
    dataset = request.app.state.relevance_store.snapshot()
    for frozen in dataset.tasks:
        for candidate in frozen.task.candidates:
            if frozen.task.material == "synthetic" and candidate.candidate_id == candidate_id:
                return TEMPLATES.TemplateResponse(request, "relevance_source.html", {"candidate": candidate})
    raise HTTPException(404, "没有这一合成来源；实际库请打开候选原始来源链接")
