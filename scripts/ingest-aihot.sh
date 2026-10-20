#!/bin/bash
# 定时增量导入：从 AIHOT 只读导入最近 DAYS 天（默认 14）发布的白名单文章。
# 只读白名单来源的行；已经导过的不变就跳过，同一篇从文件归档导过的会认成重复，不进列表。
#
#   scripts/ingest-aihot.sh
#   DAYS=30 scripts/ingest-aihot.sh
set -euo pipefail

REPO="${REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
DAYS="${DAYS:-14}"

echo "$(date '+%F %T') 开始增量导入（最近 $DAYS 天）"
docker compose --project-directory "$REPO" -f "$REPO/docker-compose.yml" \
  exec -T app research-ingest-aihot --days "$DAYS"
