#!/bin/bash
# 自动上线只构建 CI 验证的固定 SHA，不改工作区，不执行迁移。
# FORCE=1 只重试失败提交，不绕过 CI、任务锁或迁移预检。见 docs/deploy-safety.md。
set -Eeuo pipefail
REPO="${REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
export REPO
TASK=deploy
. "$REPO/scripts/status.sh"
trap 'STATUS_FAILED_LINE=$LINENO; STATUS_FAILED_CMD=$BASH_COMMAND' ERR
trap 'rc=$?; status_finish "$rc"' EXIT
run_step "自动上线" python3 "$REPO/scripts/deploy.py"
