"""环境与路径。默认域名是 investment.zldong.com。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
DEFAULT_PUBLIC_BASE_URL = "https://investment.zldong.com"
DEFAULT_BIND_PORT = 8088


def load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def assert_bind_allowed(host: str, allow_nonlocal: bool) -> None:
    if host in LOCAL_HOSTS:
        return
    if allow_nonlocal:
        return
    raise SystemExit(
        f"拒绝监听 {host}。本服务只应绑在本机回环地址。"
        "容器里如需 0.0.0.0，请同时设置 ALLOW_NONLOCAL_BIND=1，"
        "并由 docker 只把端口发布到 127.0.0.1。"
    )


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _existing_or_repo(rel: str) -> Path:
    cwd_path = Path.cwd() / rel
    if cwd_path.exists():
        return cwd_path
    return _repo_root() / rel


@dataclass(frozen=True)
class Settings:
    database_url: str
    data_dir: Path
    public_base_url: str
    bind_host: str
    bind_port: int
    allow_nonlocal_bind: bool
    sources_path: Path
    aihot_map_path: Path
    aihot_database_url: str | None
    display_timezone: str
    page_size: int = 20
    days_per_page: int = 14
    # 问库 / 摘要走 OpenAI 兼容的 chat/completions，默认是 xAI 的 Grok。
    # 凭证优先用与 AIHOT 共用的 OAuth（XAI_OAUTH_TOKEN_PATH），其次才是 XAI_API_KEY。
    # 密钥不进仓库，也不出现在 repr 里。译制另走 translate.llm，固定 xhigh。
    llm_base_url: str = "https://api.x.ai/v1"
    llm_model: str = "grok-4.7"
    llm_api_key: str = field(default="", repr=False)
    llm_reasoning_effort: str = "xhigh"
    llm_max_tokens: int = 64000
    llm_timeout: float = 900.0
    # 同一个进程里同时在跑的 Grok 请求上限（问库分批、摘要、观点、论点加起来）。
    llm_concurrency: int = 3
    # 持仓和观察名单。没有这个文件就当没有持仓。
    portfolio_path: Path | None = None
    # 首页断更提示。只看最近几天确实在做定时导入的来源，AIHOT 抓取停着、只手动导归档时不会假警报。
    # 不想要时设 RESEARCH_STALE_BANNER=0。
    stale_banner_enabled: bool = True
    # 打开公司页时是否在后台自动补论点。默认关。打开后也不限持仓：观点够数的公司都会排。
    # 需要时设 RESEARCH_THESES_AUTO=1。夜间补齐另有 THESES_ENABLED，同样默认关。
    theses_auto_enabled: bool = False

    @staticmethod
    def from_env(environ: dict[str, str] | None = None) -> Settings:
        if environ is None:
            load_dotenv(Path.cwd() / ".env")
            environ = dict(os.environ)
        data_dir = Path(environ.get("DATA_DIR", "data"))
        if not data_dir.is_absolute():
            data_dir = Path.cwd() / data_dir
        aihot = environ.get("AIHOT_DATABASE_URL", "").strip() or None
        sources = environ.get("SOURCES_FILE")
        aihot_map = environ.get("AIHOT_MAP_FILE")
        return Settings(
            database_url=environ.get(
                "DATABASE_URL",
                "postgresql://research:research@127.0.0.1:5432/research",
            ),
            data_dir=data_dir,
            public_base_url=environ.get("PUBLIC_BASE_URL", DEFAULT_PUBLIC_BASE_URL).rstrip("/"),
            bind_host=environ.get("BIND_HOST", "127.0.0.1"),
            bind_port=int(environ.get("BIND_PORT", str(DEFAULT_BIND_PORT))),
            allow_nonlocal_bind=environ.get("ALLOW_NONLOCAL_BIND", "0") == "1",
            sources_path=Path(sources) if sources else _existing_or_repo("config/sources.yaml"),
            aihot_map_path=Path(aihot_map) if aihot_map else _existing_or_repo("config/aihot_import.yaml"),
            aihot_database_url=aihot,
            display_timezone=environ.get("DISPLAY_TIMEZONE", "Asia/Shanghai"),
            page_size=int(environ.get("PAGE_SIZE", "20")),
            days_per_page=max(1, int(environ.get("DAYS_PER_PAGE", "14"))),
            llm_base_url=(environ.get("XAI_BASE_URL", "").strip() or "https://api.x.ai/v1").rstrip("/"),
            llm_model=environ.get("RESEARCH_LLM_MODEL", "").strip() or "grok-4.7",
            llm_api_key=environ.get("XAI_API_KEY", "").strip(),
            llm_reasoning_effort=environ.get("RESEARCH_LLM_REASONING", "xhigh").strip(),
            llm_max_tokens=int(environ.get("XAI_MAX_TOKENS", "64000")),
            llm_timeout=float(environ.get("RESEARCH_LLM_TIMEOUT", "900")),
            llm_concurrency=max(1, int(environ.get("RESEARCH_LLM_CONCURRENCY", "3") or "3")),
            portfolio_path=(
                Path(environ["PORTFOLIO_FILE"])
                if environ.get("PORTFOLIO_FILE")
                else _existing_or_repo("config/portfolio.yaml")
            ),
            stale_banner_enabled=environ.get("RESEARCH_STALE_BANNER", "1").strip() != "0",
            theses_auto_enabled=environ.get("RESEARCH_THESES_AUTO", "0").strip() == "1",
        )

    @property
    def public_host(self) -> str:
        return self.public_base_url.removeprefix("https://").removeprefix("http://")
