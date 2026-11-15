"""ingest.archive.safe_extract：路径穿越、符号链接、单个条目和总量上限。不连库。"""

import stat
import zipfile

import pytest

from researchlib.ingest import archive
from researchlib.ingest.archive import ArchiveTooLarge, ExtractBudget, materialize, safe_extract


def _zip(path, entries):
    with zipfile.ZipFile(path, "w") as out:
        for name, data in entries:
            out.writestr(name, data)
    return path


def test_traversal_and_absolute_entries_are_skipped(tmp_path):
    zp = _zip(
        tmp_path / "a.zip",
        [("md/ok.md", "# 好"), ("../evil.md", "x"), ("/abs.md", "x"), ("md/../../evil2.md", "x")],
    )
    dest = tmp_path / "out"
    assert safe_extract(zp, dest) == 1
    assert (dest / "md" / "ok.md").read_text() == "# 好"
    assert not (tmp_path / "evil.md").exists() and not (tmp_path / "evil2.md").exists()


def test_symlink_entries_become_plain_files(tmp_path):
    info = zipfile.ZipInfo("link.md")
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    zp = _zip(tmp_path / "a.zip", [(info, "/etc/passwd")])
    dest = tmp_path / "out"
    assert safe_extract(zp, dest) == 1
    target = dest / "link.md"
    assert not target.is_symlink() and target.read_text() == "/etc/passwd"


def test_total_bytes_and_entry_count_are_capped(tmp_path):
    zp = _zip(tmp_path / "a.zip", [(f"f{i}.md", "x" * 100) for i in range(5)])
    with pytest.raises(ArchiveTooLarge):
        safe_extract(zp, tmp_path / "o1", ExtractBudget(total_bytes=250))
    with pytest.raises(ArchiveTooLarge):
        safe_extract(zp, tmp_path / "o2", ExtractBudget(entries=3))
    assert safe_extract(zp, tmp_path / "o3", ExtractBudget(total_bytes=500, entries=5)) == 5


def test_single_entry_over_the_limit_is_dropped(tmp_path, monkeypatch):
    monkeypatch.setattr(archive, "_MAX_ENTRY_BYTES", 10)
    zp = _zip(tmp_path / "a.zip", [("big.md", "x" * 50), ("small.md", "x")])
    dest = tmp_path / "out"
    assert safe_extract(zp, dest) == 1
    assert not (dest / "big.md").exists() and (dest / "small.md").exists()


def test_directory_budget_is_shared_across_zips(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    for i in range(4):
        _zip(inbox / f"z{i}.zip", [("md/a.md", "x")])
    with pytest.raises(ArchiveTooLarge):
        materialize(inbox, tmp_path / "work", ExtractBudget(zips=3))
    roots = materialize(inbox, tmp_path / "work2")
    assert len(roots) == 5
