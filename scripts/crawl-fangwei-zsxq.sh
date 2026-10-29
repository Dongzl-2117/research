#!/bin/bash
# 方伟知识星球增量抓取 + 入库。不启 AIHOT worker。
# dl.py 把「自己所在的目录」当存档根目录，所以 dl.py 必须和数据放在一起；默认就是仓库里的
# data/inbox/fangwei-zsxq（旧的 ~/zsxq/archive/fangwei-zsxq 在 aihot 清理后已不存在，
# 只有显式给 ARCHIVE 指向别处时才用，那时才需要把结果同步回 inbox）。
# 令牌在 ~/zsxq/zsxq.env（ZSXQ_ACCESS_TOKEN），不写入仓库。
#   scripts/crawl-fangwei-zsxq.sh           # 抓取 + 入库 + 补摘要
#   scripts/crawl-fangwei-zsxq.sh --check   # 只打印路径、确认找得到 dl.py，不抓取
set -Eeuo pipefail

REPO="${REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
INBOX="$REPO/data/inbox/fangwei-zsxq"
ARCHIVE="${ARCHIVE:-$INBOX}"
DL="$ARCHIVE/dl.py"
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

# dl.py 和数据在同一目录（脚本启动不了就早点说清楚，别等 rsync 才发现没东西）。
if [ ! -f "$DL" ]; then
  echo "找不到 dl.py：${DL}（dl.py 要和存档数据放在一起，默认 ${INBOX}/dl.py；用 ARCHIVE 可以换目录）" >&2
  status_fail "找不到 dl.py：${DL}（dl.py 要和存档数据放在一起，默认 ${INBOX}/dl.py；用 ARCHIVE 可以换目录）"
  exit 2
fi

if [ "$CHECK_ONLY" -eq 1 ]; then
  echo "REPO=$REPO"
  echo "ARCHIVE=$ARCHIVE"
  echo "INBOX=$INBOX"
  echo "DL=$DL"
  echo "ZSXQ_ENV=$ZSXQ_ENV"
  if [ ! -f "$ZSXQ_ENV" ]; then
    echo "注意：没有 ${ZSXQ_ENV}，真正抓取时会失败" >&2
  fi
  echo "ok：找得到 dl.py"
  exit 0
fi

if [ ! -f "$ZSXQ_ENV" ]; then
  echo "缺少 ${ZSXQ_ENV}（ZSXQ_ACCESS_TOKEN）" >&2
  status_fail "缺少 ${ZSXQ_ENV}（ZSXQ_ACCESS_TOKEN）"
  exit 2
fi

echo "$(date '+%F %T') 开始方伟星球增量抓取"
run_step "增量抓取" env ZSXQ_ENV="$ZSXQ_ENV" python3 "$DL"

# 存档在仓库 inbox 里时不用同步；ARCHIVE 指到别处才搬过来。
if [ "$ARCHIVE" != "$INBOX" ]; then
  echo "$(date '+%F %T') 同步到 inbox"
  mkdir -p "$INBOX"
  run_step "同步" rsync -a "$ARCHIVE/md/" "$INBOX/md/"
  run_step "同步raw" rsync -a "$ARCHIVE/raw/" "$INBOX/raw/"
  run_step "同步图片" rsync -a "$ARCHIVE/images/" "$INBOX/images/"
  cp -f "$ARCHIVE/README.md" "$INBOX/README.md" 2>/dev/null || true
  cp -f "$ARCHIVE/status.txt" "$INBOX/status.txt" 2>/dev/null || true
  cp -f "$ARCHIVE/dl.py" "$INBOX/dl.py" 2>/dev/null || true
fi

echo "$(date '+%F %T') 导入 research 库"
run_step "导入" compose exec -T app research-ingest-zsxq --source fangwei-zsxq --input /data/inbox/fangwei-zsxq --origin crawl

echo "$(date '+%F %T') 补摘要（方伟星球最近缺摘要的）"
run_step "摘要" compose exec -T app research-digest --source fangwei-zsxq --limit 30 --max-minutes 40

echo "$(date '+%F %T') 补标题（空/壳/省略号，站内 flash 起题）"
run_step "标题" compose exec -T app research-repair-zsxq --source fangwei-zsxq --titles-only --ai-titles --limit 30 --spot 23999,24002,24006,24007

status_ok "方伟星球增量完成"
