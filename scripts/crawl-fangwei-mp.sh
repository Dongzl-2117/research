#!/bin/bash
# 方伟公众号增量抓取 + 入库。不启 AIHOT worker，只用极致了 key（环境变量 DAJIALA_KEY，否则仓库根 .env）。
#   scripts/crawl-fangwei-mp.sh
#   MAX_PAGES=5 scripts/crawl-fangwei-mp.sh
set -Eeuo pipefail

REPO="${REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
INBOX="$REPO/data/inbox/fangwei-mp"
MAX_PAGES="${MAX_PAGES:-3}"
TASK=crawl-fangwei-mp
. "$REPO/scripts/status.sh"
trap 'STATUS_FAILED_LINE=$LINENO; STATUS_FAILED_CMD=$BASH_COMMAND' ERR
trap 'rc=$?; status_finish "$rc"' EXIT

compose() {
  docker compose --project-directory "$REPO" -f "$REPO/docker-compose.yml" "$@"
}

# 从 .env 里取 DAJIALA_KEY 一行（去掉两端的空白、\r 和成对引号），不 source 整个文件，
# 免得把 DATABASE_URL 等变量一起导出到当前 shell。
env_file_key() {
  local file="$1" line
  [ -f "$file" ] || return 1
  line="$(grep -E '^[[:space:]]*(export[[:space:]]+)?DAJIALA_KEY[[:space:]]*=' "$file" | tail -n 1 || true)"
  [ -n "$line" ] || return 1
  line="${line#*=}"
  line="$(printf '%s' "$line" | tr -d '\r' | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
  case "$line" in
    \"*) line="${line#\"}"; line="${line%\"}" ;;
    \'*) line="${line#\'}"; line="${line%\'}" ;;
  esac
  printf '%s' "$line"
}

# 读极致了 key：优先环境变量，其次仓库根 .env（research/.env，已 gitignore；ENV_FILE 可覆盖）。
ENV_FILE="${ENV_FILE:-$REPO/.env}"
if [ -z "${DAJIALA_KEY:-}" ]; then
  DAJIALA_KEY="$(env_file_key "$ENV_FILE" || true)"
fi
if [ -z "${DAJIALA_KEY:-}" ]; then
  status_fail "缺少 DAJIALA_KEY（环境变量和 $ENV_FILE 都没有）"
  exit 2
fi

echo "$(date '+%F %T') 开始方伟公众号增量抓取（最多 ${MAX_PAGES} 页列表）"
run_step "增量抓取" env DAJIALA_KEY="$DAJIALA_KEY" python3 "$INBOX/dl.py" --incremental --max-pages "$MAX_PAGES"

echo "$(date '+%F %T') 导入 research 库"
run_step "导入" compose_exec app research-ingest-wechat --source fangwei-mp --input /data/inbox/fangwei-mp --origin crawl

# 给最近没有摘要的方伟公众号补摘要（上限 20，避免拖太久）
echo "$(date '+%F %T') 补摘要"
run_step "摘要" compose_exec app research-digest --source fangwei-mp --limit 20

status_ok "方伟公众号增量完成"
