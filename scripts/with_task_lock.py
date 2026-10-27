"""给整个宿主机脚本持共享锁，部署期间新任务安静地等下一轮。"""

import os
import subprocess
import sys
from pathlib import Path

from host_lock import host_lock

if __name__ == "__main__":
    with host_lock(Path(os.environ["REPO"])) as acquired:
        if not acquired:
            print("部署正在运行，任务等下一轮")
            raise SystemExit(0)
        raise SystemExit(subprocess.call(sys.argv[1:], env={**os.environ, "RESEARCH_TASK_LOCKED": "1"}))
