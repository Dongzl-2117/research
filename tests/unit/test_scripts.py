"""定时脚本和 launchd 模板的静态检查。

Mac mini 上的 /bin/bash 是 3.2：变量后面紧跟全角字符时，会把那几个字节算进变量名，
set -u 下直接报错（备份脚本就这样出过错，Linux 上的 bash 5 测不出来）。这里逐行查，
要求一律写成 ${变量}。
"""

import importlib.util
import plistlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = sorted((ROOT / "scripts").glob("*.sh"))
PLISTS = sorted((ROOT / "deploy" / "launchd").glob("*.plist"))
_GLUED = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*[^\x00-\x7f]")


def test_there_are_scripts_to_check():
    assert SCRIPTS and PLISTS


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda path: path.name)
def test_no_variable_glued_to_non_ascii(path: Path):
    bad = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if line.lstrip().startswith("#"):
            continue
        if _GLUED.search(line):
            bad.append(f"{path.name}:{number}: {line.strip()}")
    assert not bad, "变量后面紧跟中文，写成 ${变量}：\n" + "\n".join(bad)


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda path: path.name)
def test_scripts_parse(path: Path):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("没有 bash")
    result = subprocess.run([bash, "-n", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("path", [path for path in SCRIPTS if path.name != "status.sh"], ids=lambda path: path.name)
def test_scripts_are_executable(path: Path):
    # 文档里都写成直接跑 scripts/xxx.sh；status.sh 只用 . 引入，不单独跑。
    assert path.stat().st_mode & 0o100, f"{path.name} 没有执行权限：git update-index --chmod=+x scripts/{path.name}"


@pytest.mark.parametrize("path", PLISTS, ids=lambda path: path.name)
def test_launchd_templates_are_valid_plists(path: Path):
    filled = (
        path.read_text(encoding="utf-8")
        .replace("__REPO__", "/Users/me/research")
        .replace("__BACKUP_DIR__", "/Users/me/research-backups")
        .replace("__LOGS__", "/Users/me/Library/Logs/research")
        .replace("__PATH__", "/opt/homebrew/bin:/usr/bin:/bin")
        .replace("__NIGHTLY_OCR__", "0")
    )
    assert "__" not in re.sub(r"<!--.*?-->", "", filled, flags=re.S)
    data = plistlib.loads(filled.encode("utf-8"))
    assert data["Label"] == path.stem
    assert data["ProgramArguments"][0] == "/bin/bash"
    script = data["ProgramArguments"][1].replace("/Users/me/research/", "")
    assert (ROOT / script).is_file()


def test_ci_failure_becomes_one_annotation_with_the_tail(capsys):
    spec = importlib.util.spec_from_file_location("ci_annotate", ROOT / "tools" / "ci" / "annotate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    failing = "import sys; print('第一行'); print('50%：失败, 见上'); sys.exit(3)"
    assert module.main(["单元测试", "--", sys.executable, "-c", failing]) == 3
    out = capsys.readouterr().out
    assert "第一行\n" in out
    assert "::error title=单元测试（退出码 3）::第一行%0A50%25：失败, 见上" in out
    assert module.main(["好的", "--", sys.executable, "-c", "print('ok')"]) == 0
    assert "::error" not in capsys.readouterr().out


def _nightly(tmp_path: Path, **env: str) -> tuple[list[str], dict]:
    """拿假的 docker 跑一遍 nightly.sh，返回每条 docker 命令和写出的状态。"""
    import json
    import os

    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("没有 bash")
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    for name in ("nightly.sh", "status.sh"):
        shutil.copy(ROOT / "scripts" / name, repo / "scripts" / name)
    (repo / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "docker.log"
    fake = bin_dir / "docker"
    fake.write_text(f'#!/bin/bash\necho "$*" >> "{log}"\n', encoding="utf-8")
    fake.chmod(0o755)
    run_env = {
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "REPO": str(repo),
        "STATUS_DIR": str(tmp_path / "status"),
        "TMPDIR": str(tmp_path),
        **env,
    }
    subprocess.run([bash, str(repo / "scripts" / "nightly.sh")], env=run_env, capture_output=True, text=True)
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    status = json.loads((tmp_path / "status" / "nightly.json").read_text(encoding="utf-8"))
    return calls, status


def test_nightly_views_in_parallel_and_theses_for_the_portfolio(tmp_path: Path):
    """默认：观点同时抽 3 篇；论点只补持仓和观察名单里的公司（research-theses --portfolio）。"""
    calls, status = _nightly(tmp_path)
    views = [call for call in calls if "research-views" in call]
    assert len(views) == 1 and "--limit 60" in views[0] and "--workers 3" in views[0]
    assert "DEEPSEEK_REASONING_EFFORT" not in views[0]
    assert [call for call in calls if "research-theses" in call] == [
        call for call in calls if call.endswith("research-theses --portfolio")
    ]
    assert any(call.endswith("research-theses --portfolio") for call in calls)
    assert status["ok"] is True and "论点（持仓和观察）" in status["message"]


def test_nightly_old_theses_switch_and_views_effort(tmp_path: Path):
    calls, status = _nightly(tmp_path / "all", THESES_ENABLED="1", VIEWS_EFFORT="high", VIEWS_WORKERS="1")
    assert any(call.endswith("research-theses") for call in calls)
    views = next(call for call in calls if "research-views" in call)
    assert "-e DEEPSEEK_REASONING_EFFORT=high" in views and "--workers 1" in views
    calls, status = _nightly(tmp_path / "off", THESES_ENABLED="0")
    assert not any("research-theses" in call for call in calls)
    assert "论点" not in status["message"]
    calls, status = _nightly(tmp_path / "typo", THESES_SCOPE="portfolios")
    assert not any("research-theses" in call for call in calls)
    assert status["ok"] is False and "THESES_SCOPE=portfolios" in status["message"]

