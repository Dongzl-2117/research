import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from researchlib import llm_batch, ocr


@pytest.mark.parametrize(
    ("done", "failed", "skipped", "code"), [(0, 0, 0, 0), (0, 0, 3, 0), (0, 2, 1, 4), (1, 2, 0, 0)])
def test_nightly_exit_distinguishes_empty_from_all_failed(done, failed, skipped, code, capsys):
    report = {"done": done, "failed": failed, "skipped": skipped, "fatal": "", "paused": ""}
    assert llm_batch.exit_code(report, fail_empty=True) == code
    assert f"写入 {done} 篇，失败 {failed} 篇" in capsys.readouterr().err
    assert llm_batch.exit_code(report) == 0


@pytest.mark.parametrize(("succeeded", "bad", "missing", "expected"), [(0, 0, 0, 0), (0, 2, 0, 4), (0, 0, 2, 4),
                                                                     (1, 2, 2, 0)])
def test_nightly_ocr_tracks_success_even_if_no_text(succeeded, bad, missing, expected):
    report = {"recognized": succeeded, "failed": 0, "invalid": bad, "missing": missing, "with_text": 0}
    assert ocr.night_exit_code(report) == expected


def test_media_restoration_checks_actual_bytes_and_lists_failed_rows(tmp_path):
    directory = tmp_path / "images"
    directory.mkdir()
    (directory / "a.png").write_bytes(b"pixels")
    checksum = hashlib.sha256(b"pixels").hexdigest()
    script = Path(__file__).resolve().parents[2] / "scripts" / "verify_restore_images.py"

    def check(names):
        manifest = "".join(json.dumps([name, checksum]) + "\n" for name in names)
        return subprocess.run([sys.executable, str(script), str(directory)], input=manifest, text=True,
                              capture_output=True, check=False)

    assert check([]).returncode == 0
    assert check(["a.png"]).returncode == 0
    (directory / "a.png").write_bytes(b"changed")
    result = check(["a.png", "missing.png", "../backup.log"])
    assert result.returncode == 1 and "异常 3 个" in result.stdout
    assert "内容哈希不一致" in result.stderr and "备份目录" in result.stderr
