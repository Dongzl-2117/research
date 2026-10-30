#!/bin/bash
# 孤独进化体公众号每天抓一次：research-crawl-wechat 向极致了查最新文章（每次约 0.14 元），新文章的正文从公众号原文页取
# （取不到才用极致了的正文，每篇约 0.03 元），写进 data/inbox/gudujinhuati 并入库；再下载外链图片、给新文章补摘要。
# 极致了 key 和方伟公众号共用：仓库根 .env 的 DAJIALA_KEY，docker compose 透传进容器。
#   scripts/crawl-gudujinhuati.sh
set -Eeuo pipefail

REPO="${REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
SOURCE=gudujinhuati
TASK=crawl-gudujinhuati
. "$REPO/scripts/status.sh"
trap 'STATUS_FAILED_LINE=$LINENO; STATUS_FAILED_CMD=$BASH_COMMAND' ERR
trap 'rc=$?; status_finish "$rc"' EXIT

compose() {
  docker compose --project-directory "$REPO" -f "$REPO/docker-compose.yml" "$@"
}

echo "$(date '+%F %T') 开始孤独进化体抓取"
run_step "抓取并导入" compose_exec app research-crawl-wechat --source "$SOURCE"

echo "$(date '+%F %T') 下载外链图片"
run_step "下载外链图片" compose_exec app research-fetch-images --limit 200

# 给新文章补摘要（上限 5，平时一两天才有一篇）
echo "$(date '+%F %T') 补摘要"
run_step "摘要" compose_exec app research-digest --source "$SOURCE" --limit 5

status_ok "孤独进化体抓取完成"
