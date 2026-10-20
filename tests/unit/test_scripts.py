"""定时脚本和 launchd 模板的静态检查。

Mac mini 上的 /bin/bash 是 3.2：变量后面紧跟全角字符时，会把那几个字节算进变量名，
set -u 下直接报错（备份脚本就这样出过错，Linux 上的 bash 5 测不出来）。这里逐行查，
要求一律写成 ${变量}。
"""

import plistlib
import re
import shutil
import subprocess
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
