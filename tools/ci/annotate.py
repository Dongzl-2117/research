"""CI 里跑一条命令：输出照常打印；失败时把最后几十行做成一条错误注释。

注释会挂在这次提交的检查结果上，PR 页面和 GitHub API 都能直接看到是哪一步、为什么失败，
不用翻完整的日志。

    python tools/ci/annotate.py "单元测试" -- pytest tests/unit
"""

from __future__ import annotations

import subprocess
import sys
from collections import deque

TAIL_LINES = 60
MAX_CHARS = 8000


def escape_data(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def escape_property(text: str) -> str:
    return escape_data(text).replace(":", "%3A").replace(",", "%2C")


def annotation(title: str, lines) -> str:
    message = "\n".join(lines)[-MAX_CHARS:]
    return f"::error title={escape_property(title)}::{escape_data(message)}"


def main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[1] != "--":
        print("用法：annotate.py 标题 -- 命令 参数…", file=sys.stderr)
        return 2
    title, command = argv[0], argv[2:]
    tail: deque[str] = deque(maxlen=TAIL_LINES)
    with subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace", bufsize=1
    ) as process:
        assert process.stdout is not None
        for line in process.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            tail.append(line.rstrip("\n"))
        code = process.wait()
    if code != 0:
        print(annotation(f"{title}（退出码 {code}）", tail), flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
