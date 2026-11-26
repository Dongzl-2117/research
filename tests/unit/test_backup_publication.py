"""备份公开时机、二进制输出和新旧恢复篇数。快照本身另用 PostgreSQL 核验。"""

import json
import os
import shutil
import subprocess
from pathlib import Path

from researchlib import health

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
DUMP = b"PGDMP\x00\xff\n\x19"


def test_only_completed_backups_are_selected_and_verified(tmp_path):
    repo = tmp_path / "repo"
    (repo / "data/images").mkdir(parents=True)
    (repo / "scripts").symlink_to(SCRIPTS, target_is_directory=True)
    commands = tmp_path / "bin"
    commands.mkdir()
    docker = commands / "docker"
    docker.write_text(
        """#!/usr/bin/env python3
import os, sys
args = sys.argv[sys.argv.index("db") + 1:]
if args[0] == "psql" and "-XqAt" in args:
    cmd = sys.stdin.read()
    assert "pg_export_snapshot()" in cmd and '--snapshot="$snap"' in cmd
    sys.stdout.buffer.write(b"1 t\\n" + b"PGDMP\\x00\\xff\\n\\x19")
elif args[0] == "psql" and any("count(*)" in a for a in args):
    print(os.environ["RESTORED"])
elif args[0] == "pg_restore":
    assert sys.stdin.buffer.read() == b"PGDMP\\x00\\xff\\n\\x19"
elif args[0] not in ("psql", "dropdb", "createdb"):
    sys.exit(2)
"""
    )
    rsync = commands / "rsync"
    rsync.write_text('#!/bin/sh\nexit "${SYNC_EXIT:-0}"\n')
    for command in (docker, rsync):
        command.chmod(0o755)
    backup = tmp_path / "backup"
    env = os.environ | {"REPO": str(repo), "BACKUP_DIR": str(backup), "PATH": f"{commands}:{os.environ['PATH']}"}

    def run(task, **options):
        return subprocess.run(["bash", SCRIPTS / task], env=env | options, capture_output=True)

    (repo / "data/images").rmdir()
    assert run("backup.sh").returncode == 1
    assert list((backup / "db").iterdir()) == []
    (repo / "data/images").mkdir()
    assert run("backup.sh", SYNC_EXIT="23").returncode == 23
    assert list((backup / "db").iterdir()) == []
    # 首份导出没有历史估值。导出、同步和发布都成功后，模拟目的盘余量只够 6 字节。
    (tmp_path / "sitecustomize.py").write_text(
        "import shutil\n"
        "from types import SimpleNamespace\n"
        "shutil.disk_usage = lambda path: SimpleNamespace(free=6)\n"
    )
    assert run("backup.sh", PYTHONPATH=str(tmp_path)).returncode == 0
    status = json.loads((repo / "data/status/backup.json").read_text())
    assert status["ok"] and "可用空间" in status["warning"]
    assert any("备份完成时" in n for n in health.notices(repo / "data", "Asia/Shanghai"))
    archive, = (backup / "db").glob("*.dump")
    assert archive.read_bytes() == DUMP
    counts = Path(str(archive) + ".articles")
    assert counts.read_text().strip() == "1"
    assert run("restore-check.sh", RESTORED="1").returncode == 0
    assert run("restore-check.sh", RESTORED="2").returncode == 1
    legacy = backup / "db/research-20261001-120000.dump"
    archive.rename(legacy)
    counts.rename(Path(str(legacy) + ".articles"))
    assert run("restore-check.sh", RESTORED="2").returncode == 0
    assert run("restore-check.sh", RESTORED="0").returncode == 1
    assert run("backup.sh").returncode == 0
    assert "warning" not in json.loads((repo / "data/status/backup.json").read_text())


def test_backup_checks_destination_capacity_before_starting_dump(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "scripts").symlink_to(SCRIPTS, target_is_directory=True)
    destination = tmp_path / "备份 空间"
    dumps = destination / "db"
    dumps.mkdir(parents=True)
    archive = dumps / "research-20261119-031702.dump"
    # 稀疏文件只提供旧导出的逻辑大小，测试不占满磁盘。
    with archive.open("wb") as stream:
        stream.truncate(shutil.disk_usage(dumps).free + 100 * 1024**2)
    commands = tmp_path / "bin"
    commands.mkdir()
    docker = commands / "docker"
    docker.write_text('#!/bin/sh\ntouch "$REPO/started-dump"\nexit 99\n')
    docker.chmod(0o755)
    env = os.environ | {"REPO": str(repo), "BACKUP_DIR": str(destination), "PATH": f"{commands}:{os.environ['PATH']}"}
    run = subprocess.run(["bash", SCRIPTS / "backup.sh"], env=env, capture_output=True)
    assert run.returncode != 0
    assert not (repo / "started-dump").exists()
    assert list(dumps.iterdir()) == [archive]
    status = json.loads((repo / "data/status/backup.json").read_text())
    assert status["ok"] is False and "可用空间" in status["message"]

    retry = subprocess.run(["bash", SCRIPTS / "backup.sh"], env=env | {"BACKUP_EXPECTED_MIB": "1"}, capture_output=True)
    assert retry.returncode != 0 and (repo / "started-dump").exists()
