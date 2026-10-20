#!/bin/bash
# 定时增量导入：从 AIHOT 只读导入最近 DAYS 天（默认 14）发布的白名单文章。
# 只读白名单来源的行；已经导过的不变就跳过，同一篇从文件归档导过的会认成重复，不进列表。
# 导入完顺手把新文章正文里的外链图片下载到本地（IMAGES_LIMIT，默认 200 张，0 表示不下）。
# 结果写进 data/status/ingest-aihot.json，失败或六小时没成功时首页提示。
#
#   scripts/ingest-aihot.sh
#   DAYS=30 scripts/ingest-aihot.sh
set -Eeuo pipefail

REPO="${REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
DAYS="${DAYS:-14}"
IMAGES_LIMIT="${IMAGES_LIMIT:-200}"
TASK=ingest-aihot
. "$REPO/scripts/status.sh"
trap 'STATUS_FAILED_LINE=$LINENO; STATUS_FAILED_CMD=$BASH_COMMAND' ERR
trap 'rc=$?; status_finish "$rc"' EXIT

compose() {
  docker compose --project-directory "$REPO" -f "$REPO/docker-compose.yml" "$@"
}

echo "$(date '+%F %T') 开始增量导入（最近 ${DAYS} 天）"
run_step "导入" compose exec -T app research-ingest-aihot --days "$DAYS"
if [ "$IMAGES_LIMIT" -gt 0 ]; then
  run_step "下载外链图片" compose exec -T app research-fetch-images --limit "$IMAGES_LIMIT"
fi
status_ok "最近 ${DAYS} 天"
