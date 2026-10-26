"""仓库结构的静态检查。"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_test_modules_have_unique_names():
    # tests/ 下没有 __init__.py，unit 和 integration 里有同名模块时，直接跑 pytest 收集就报错。
    # CI 把两边分开跑，查不出来。
    names = [path.name for path in (ROOT / "tests").rglob("test_*.py")]
    assert sorted({name for name in names if names.count(name) > 1}) == []


def test_agent_guide_is_where_agents_look():
    # Codex 等读 AGENTS.md，Claude Code 读 CLAUDE.md；CLAUDE.md 只引用 AGENTS.md，免得两份说法不一样。
    assert (ROOT / "AGENTS.md").is_file()
    assert (ROOT / "CLAUDE.md").read_text(encoding="utf-8").strip() == "@AGENTS.md"
