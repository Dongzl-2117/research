"""在宿主机上检查备份目的盘，用最近一份已公布导出的实际字节数估计下一份。"""

import shutil
import sys
from pathlib import Path

MIB = 1024**2


def capacity_warning(directory, expected_mib):
    free = shutil.disk_usage(directory).free
    if expected_mib:
        if not expected_mib.isascii() or not expected_mib.isdigit() or int(expected_mib) < 1:
            raise ValueError("BACKUP_EXPECTED_MIB 要填正整数（MiB）")
        required = int(expected_mib) * MIB
        estimate = "指定的预计导出大小"
    else:
        latest = max((p for p in directory.glob("research-*.dump") if p.is_file()), default=None)
        if latest is None:
            return "" if free else "备份盘没有可用空间"
        required = latest.stat().st_size
        estimate = "最近一份导出大小"
    if not free:
        return "备份盘没有可用空间"
    if free >= required:
        return ""
    return f"备份盘可用空间 {free // MIB} MiB，{estimate}约 {(required + MIB - 1) // MIB} MiB"


if __name__ == "__main__":
    try:
        warning = capacity_warning(Path(sys.argv[1]), sys.argv[2] if len(sys.argv) > 2 else "")
    except (OSError, ValueError) as exc:
        sys.exit(f"无法核对备份盘空间：{exc}")
    if warning:
        sys.exit(warning)
