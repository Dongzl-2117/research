"""备份公开时机、二进制输出和新旧恢复篇数。快照本身另用 PostgreSQL 核验。"""

import os
import subprocess
from pathlib import Path

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
    assert run("backup.sh").returncode == 0
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
