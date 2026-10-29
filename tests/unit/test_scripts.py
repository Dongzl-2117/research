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


def test_nightly_starts_app_before_ocr():
    """识图在 app 容器里跑。容器没起来时先 up，避免 service app is not running。"""
    nightly = (ROOT / "scripts" / "nightly.sh").read_text(encoding="utf-8")
    status = (ROOT / "scripts" / "status.sh").read_text(encoding="utf-8")
    assert "compose_exec app research-ocr" in nightly
    assert "compose up -d" in status
    assert "is not running" in status
    for name in ("crawl-fangwei-mp.sh", "crawl-fangwei-zsxq.sh"):
        text = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert "compose_exec app research-ingest" in text


def test_nightly_theses_disabled_by_default():
    """论点夜间步骤默认关：没设 THESES_ENABLED=1 时不跑 research-theses。"""
    source = (ROOT / "scripts" / "nightly.sh").read_text(encoding="utf-8")
    assert 'THESES_ENABLED="${THESES_ENABLED:-0}"' in source
    assert 'if [ "$THESES_ENABLED" = "1" ]; then' in source
    assert "research-theses" in source
    # 默认关时 status_ok 不硬编码带「论点」。
    assert 'status_ok "外链图片、观点、摘要、论点' not in source

