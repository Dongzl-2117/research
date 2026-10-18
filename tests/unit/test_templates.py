from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "src" / "researchlib" / "web" / "templates"


def _env():
    return Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html"]))


def test_search_template_keeps_author_count_markup():
    html = _env().get_template("search.html").render(
        title="搜索 腾讯",
        canonical="https://investment.zldong.com/search?q=%E8%85%BE%E8%AE%AF",
        public_host="investment.zldong.com",
        query="腾讯",
        nav="search",
        sample_banner=False,
        sources=[],
        active_source="",
        author_counts=[{"name": "方伟", "count": 3}, {"name": "乘风破浪", "count": 0}],
        source_counts=[{"id": "fangwei-mp", "name": "方伟公众号", "count": 3}],
        results=[],
        total=0,
    )
    assert "方伟</span><strong>3</strong>" in html
    assert "乘风破浪</span><strong>0</strong>" in html
    assert "investment.zldong.com" in html
    assert "research.zldong.com" not in html
