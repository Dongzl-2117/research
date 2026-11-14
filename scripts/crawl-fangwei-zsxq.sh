#!/bin/bash
# 方伟知识星球增量抓取 + 入库。不启 AIHOT worker。
# 抓取脚本是仓库里的 scripts/zsxq/dl.py（11-14 以前只放在存档目录里，审核 O3）。数据写进 ZSXQ_ARCHIVE
# 指的存档目录，默认就是 data/inbox/fangwei-zsxq；ARCHIVE 指向别处时再把结果同步回 inbox。
# 令牌（ZSXQ_ACCESS_TOKEN）和星球 ID（ZSXQ_GROUP_ID）在 ~/zsxq/zsxq.env，不写入仓库。
#   scripts/crawl-fangwei-zsxq.sh           # 抓取 + 入库 + 补摘要
#   scripts/crawl-fangwei-zsxq.sh --check   # 只打印路径、确认找得到 dl.py 和星球 ID，不抓取
set -Eeuo pipefail

REPO="${REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
INBOX="$REPO/data/inbox/fangwei-zsxq"
ARCHIVE="${ARCHIVE:-$INBOX}"
DL="${DL:-$REPO/scripts/zsxq/dl.py}"
ZSXQ_ENV="${ZSXQ_ENV:-$HOME/zsxq/zsxq.env}"
TASK=crawl-fangwei-zsxq
. "$REPO/scripts/status.sh"
trap 'STATUS_FAILED_LINE=$LINENO; STATUS_FAILED_CMD=$BASH_COMMAND' ERR
trap 'rc=$?; status_finish "$rc"' EXIT

CHECK_ONLY=0
case "${1:-}" in
  --check) CHECK_ONLY=1 ;;
  "") ;;
  *) echo "用法：$0 [--check]" >&2; exit 2 ;;
esac

compose() {
  docker compose --project-directory "$REPO" -f "$REPO/docker-compose.yml" "$@"
}

# 脚本启动不了就早点说清楚，别等 rsync 才发现没东西。
if [ ! -f "$DL" ]; then
  echo "找不到 dl.py：${DL}（默认是仓库里的 scripts/zsxq/dl.py）" >&2
  status_fail "找不到 dl.py：${DL}（默认是仓库里的 scripts/zsxq/dl.py）"
  exit 2
fi

# 星球 ID 不在仓库里：环境变量 ZSXQ_GROUP_ID，或 zsxq.env 里的 ZSXQ_GROUP_ID=...
has_group_id() {
  [ -n "${ZSXQ_GROUP_ID:-}" ] && return 0
  [ -f "$ZSXQ_ENV" ] && grep -q '^ZSXQ_GROUP_ID=.' "$ZSXQ_ENV"
}

if [ "$CHECK_ONLY" -eq 1 ]; then
  echo "REPO=$REPO"
  echo "ARCHIVE=$ARCHIVE"
  echo "INBOX=$INBOX"
  echo "DL=$DL"
  echo "ZSXQ_ENV=$ZSXQ_ENV"
  if [ ! -f "$ZSXQ_ENV" ]; then
    echo "注意：没有 ${ZSXQ_ENV}，真正抓取时会失败" >&2
  fi
  if ! has_group_id; then
    echo "注意：${ZSXQ_ENV} 里没有 ZSXQ_GROUP_ID=...，真正抓取时会失败" >&2
  fi
  echo "ok：找得到 dl.py"
  exit 0
fi

if [ ! -f "$ZSXQ_ENV" ]; then
  echo "缺少 ${ZSXQ_ENV}（ZSXQ_ACCESS_TOKEN）" >&2
  status_fail "缺少 ${ZSXQ_ENV}（ZSXQ_ACCESS_TOKEN）"
  exit 2
fi
if ! has_group_id; then
  echo "缺少星球 ID：在 ${ZSXQ_ENV} 里加一行 ZSXQ_GROUP_ID=...（方伟星球的 group_id）" >&2
  status_fail "缺少星球 ID：在 ${ZSXQ_ENV} 里加一行 ZSXQ_GROUP_ID=...（方伟星球的 group_id）"
  exit 2
fi

echo "$(date '+%F %T') 开始方伟星球增量抓取"
run_step "增量抓取" env ZSXQ_ENV="$ZSXQ_ENV" ZSXQ_ARCHIVE="$ARCHIVE" python3 "$DL"

# 存档在仓库 inbox 里时不用同步；ARCHIVE 指到别处才搬过来。
if [ "$ARCHIVE" != "$INBOX" ]; then
  echo "$(date '+%F %T') 同步到 inbox"
  mkdir -p "$INBOX"
  run_step "同步" rsync -a "$ARCHIVE/md/" "$INBOX/md/"
  run_step "同步raw" rsync -a "$ARCHIVE/raw/" "$INBOX/raw/"
  run_step "同步图片" rsync -a "$ARCHIVE/images/" "$INBOX/images/"
  cp -f "$ARCHIVE/README.md" "$INBOX/README.md" 2>/dev/null || true
  cp -f "$ARCHIVE/status.txt" "$INBOX/status.txt" 2>/dev/null || true
fi

echo "$(date '+%F %T') 导入 research 库"
run_step "导入" compose_exec app research-ingest-zsxq --source fangwei-zsxq --input /data/inbox/fangwei-zsxq --origin crawl

echo "$(date '+%F %T') 补摘要（方伟星球最近缺摘要的）"
run_step "摘要" compose_exec app research-digest --source fangwei-zsxq --limit 30 --max-minutes 40

echo "$(date '+%F %T') 补标题（空/壳/省略号，站内 flash 起题）"
run_step "标题" compose_exec app research-repair-zsxq --source fangwei-zsxq --titles-only --ai-titles --limit 30

status_ok "方伟星球增量完成"
