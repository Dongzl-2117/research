"""独立本机实验路由；不挂入主站，不提供远程认证。"""
from __future__ import annotations

import ipaddress
import secrets
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates

from researchlib.experiments.engine import compare, run_id
from researchlib.experiments.schema import (
    Answer,
    CompareRequest,
    GenerateRequest,
    RatingRequest,
    Snapshot,
    Tuning,
    canonical,
    digest,
)
from researchlib.experiments.store import Conflict, Store
from researchlib.sources import enabled_sources, load_sources
from researchlib.web.relevance import FORWARDED_HEADERS

ROOT = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(ROOT / "templates"))


def create_experiment_app(snapshot: Snapshot, db_path: Path, *, sources_path: Path | None = None,
                          generator=None, generator_label: str | None = None):
    if generator and not generator_label:
        raise ValueError("生成适配器必须提供版本化身份")
    store = Store(db_path, snapshot.fingerprint)
    csrf = secrets.token_urlsafe(32)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.experiment_csrf = csrf

    def check_sources():
        if snapshot.material == "synthetic":
            return
        if not sources_path:
            raise ValueError("私有快照需显式提供当前来源配置，不能只信旧快照权限")
        active = {s.id: asdict(s) for s in enabled_sources(load_sources(sources_path))}
        if any(active.get(s.id) != asdict(s) for s in snapshot.sources):
            raise ValueError("来源已停用或策略已变更；请重新捕获快照")
    check_sources()

    @app.middleware("http")
    async def local_access(request: Request, call_next):
        try:
            local = bool(request.client and ipaddress.ip_address(request.client.host).is_loopback)
        except ValueError:
            local = False
        origin = request.headers.get("origin")
        if (not local or request.url.hostname not in {"127.0.0.1", "localhost", "::1"}
                or FORWARDED_HEADERS.intersection(request.headers)
                or (origin and origin != str(request.base_url).rstrip("/"))
                or request.headers.get("sec-fetch-site") == "cross-site"):
            response = JSONResponse({"detail": "实验台只允许直接连接本机，拒绝跨站或代理请求"}, status_code=403)
        elif request.method not in {"GET", "HEAD"} and not secrets.compare_digest(
            request.headers.get("x-experiment-csrf", ""), csrf
        ):
            response = JSONResponse({"detail": "缺少本机实验页面 CSRF 凭据"}, status_code=403)
        else:
            try:
                check_sources()
            except (ValueError, OSError):
                response = JSONResponse({"detail": "来源授权已变更，请停止服务并重新捕获快照"}, status_code=403)
            else:
                body = bytearray()
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body) > 65536:
                        break
                if len(body) > 65536:
                    response = JSONResponse({"detail": "请求过大"}, status_code=413)
                else:
                    request._body = bytes(body)
                    response = await call_next(request)
        response.headers.update({
            "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY", "X-Robots-Tag": "noindex, nofollow",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        })
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

    @app.get("/experiments")
    def page(request: Request):
        return TEMPLATES.TemplateResponse(request, "experiments.html", {"csrf": csrf})

    @app.get("/experiments/static/{filename}")
    def static(filename: str):
        if filename not in {"experiments.js", "experiments.css"}:
            raise HTTPException(404)
        return FileResponse(ROOT / "static" / filename)

    @app.get("/api/experiments/state")
    def state():
        return {
            "snapshot_id": snapshot.fingerprint, "material": snapshot.material,
            "created_at": snapshot.created_at.isoformat(), "recipe": snapshot.recipe,
            "cases": [{"id": c.spec.id, "query": c.spec.query, "scope": c.spec.scope.model_dump(mode="json")}
                      for c in snapshot.cases],
            "defaults": Tuning().model_dump(), "history": store.history(),
            "generator": generator_label if generator else None,
        }

    @app.post("/api/experiments/compare")
    def run(body: CompareRequest):
        key = run_id(snapshot, body)
        try:
            return store.get(key)
        except LookupError:
            return store.save(compare(snapshot, body))

    @app.get("/api/experiments/runs/{key}")
    def get_run(key: str):
        return store.export(key)

    @app.post("/api/experiments/runs/{key}/ratings")
    def rate(key: str, body: RatingRequest):
        return store.label(key, body)

    @app.get("/api/experiments/runs/{key}/export")
    def export(key: str):
        return Response(canonical(store.export(key)), media_type="application/json", headers={
            "Content-Disposition": 'attachment; filename="algorithm-comparison.json"',
        })

    @app.get("/api/experiments/snapshot")
    def export_snapshot():
        return Response(canonical(snapshot.model_dump(mode="json")), media_type="application/json", headers={
            "Content-Disposition": 'attachment; filename="algorithm-snapshot.json"',
        })

    @app.get("/api/experiments/source/{case_id}/{article_id}/{ordinal}")
    def source(case_id: str, article_id: int, ordinal: int):
        case = next((c for c in snapshot.cases if c.spec.id == case_id), None)
        if case is None:
            raise LookupError("问题不存在")
        row = next((r for r in case.rows + case.qa_rows + case.neighbours
                    if r.article_id == article_id and r.ord == ordinal), None)
        if row is None:
            raise LookupError("原文不在授权快照中")
        payload = row.model_dump(mode="json")
        return payload | {"snapshot_id": snapshot.fingerprint, "content_hash": digest(payload)}

    @app.post("/api/experiments/runs/{key}/generate")
    def generate(key: str, body: GenerateRequest):
        if generator is None:
            raise HTTPException(503, "未配置生成适配器；可导出两边提示词，当前不会调用任何模型")
        run = store.get(key)
        case = next((c for c in run["cases"] if c["id"] == body.case_id), None)
        if case is None:
            raise ValueError("问题不属于这次对照")
        if not all(case[side]["generation_eligible"] for side in ("baseline", "candidate")):
            raise ValueError("某一侧无材料或提示词超限；请调整参数后重新对照")
        operation, claimed = store.claim_generation(key, body.case_id, body.request_id, generator_label)
        if not claimed:
            return operation
        for side in ("baseline", "candidate"):
            operation["attempted_sides"].append(side)
            store.save_generation(key, body.case_id, operation)
            try:
                answer = Answer.model_validate(generator(messages=case[side]["messages"], max_output_tokens=1500))
                operation["sides"][side] = answer.model_dump()
                store.save_generation(key, body.case_id, operation)
            except Exception:
                # 已发生的调用可能计费；保留占位和成功侧，断线或失败均不自动再次调用。
                operation["status"] = "failed"
                operation["error"] = f"{side} 生成失败，结果可能不完整；此对照不会自动重试"
                store.save_generation(key, body.case_id, operation)
                return operation
        operation["status"] = "done"
        store.save_generation(key, body.case_id, operation)
        return operation

    return app
