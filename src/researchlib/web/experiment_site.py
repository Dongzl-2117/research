"""主站管理入口装配；没有被审核的所有者认证适配器时拒绝挂载。"""
from __future__ import annotations

from researchlib.experiments.schema import Snapshot
from researchlib.web.experiment_access import ADMIN_MOUNT, ADMIN_PAGE, verified_owner


def attach_experiment_admin(app, settings, verifier):
    if not callable(verifier):
        raise ValueError("网站实验开关已开启，但未接入可核验的所有者认证，拒绝启动")
    if not settings.experiments_snapshot or not settings.experiments_db:
        raise ValueError("网站实验需显式提供已审核的私有快照和独立 SQLite")
    snapshot = Snapshot.model_validate_json(settings.experiments_snapshot.read_text())
    from researchlib.web.experiments import create_experiment_app

    generator = None
    if settings.experiments_site_model:
        from researchlib.experiments.site_model import SiteModelGenerator

        generator = SiteModelGenerator(settings)
    experiment = create_experiment_app(
        snapshot, settings.experiments_db, sources_path=settings.sources_path,
        website_verifier=verifier, public_origin=settings.public_base_url,
        generator=generator, generator_label=generator.label if generator else None,
        daily_limit=settings.experiments_daily_pairs if generator else None,
    )
    app.mount(ADMIN_MOUNT, experiment, name="experiment-admin")
    permitted = frozenset(s.id for s in snapshot.sources)

    @app.middleware("http")
    async def owner_navigation(request, call_next):
        # 状态只来自服务端验证结果，不能由请求中的 owner 或代理头直接设置。
        owner = await verified_owner(verifier, request)
        request.state.experiment_admin_url = ADMIN_PAGE if owner and permitted <= owner.source_ids else None
        return await call_next(request)
