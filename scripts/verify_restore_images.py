"""逐行读取恢复库的 [相对路径, SHA256]，核对备份盘上的原图。仅用宿主机标准库。"""

import hashlib
import json
import re
import sys
from pathlib import Path


def verify(stream, directory):
    root = Path(directory).resolve()
    checked = errors = 0
    for line in stream:
        checked += 1
        try:
            name, expected = json.loads(line)
            if not isinstance(name, str) or not isinstance(expected, str) or not re.fullmatch("[a-f0-9]{64}", expected):
                raise ValueError("引用的路径或 SHA256 无效")
            path = (root / name).resolve()
            if Path(name).is_absolute() or root not in path.parents:
                raise ValueError("引用的文件不在图片备份目录中")
            digest = hashlib.sha256()
            with path.open("rb") as source:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(block)
            if digest.hexdigest() != expected:
                raise ValueError(f"{name} 内容哈希不一致")
        except (ValueError, OSError, RuntimeError, TypeError) as exc:
            errors += 1
            if errors <= 10:
                print(f"第 {checked} 个图片引用：{exc}", file=sys.stderr)
    print(f"核对了 {checked} 个图片引用，异常 {errors} 个")
    return int(bool(errors))


if __name__ == "__main__":
    sys.exit(verify(sys.stdin, sys.argv[1]))
