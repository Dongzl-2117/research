#!/bin/bash
# 每月试恢复一次：把最新一份导出恢复到临时库 research_restore_check，核对篇数，然后删掉临时库。
# 只是演练，不碰正式库。恢复不出来、或者篇数对不上，退出码非 0，日志里写原因。
#
#   BACKUP_DIR=/Volumes/备份盘/research scripts/restore-check.sh
set -euo pipefail

REPO="${REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
BACKUP_DIR="${BACKUP_DIR:-}"
CHECK_DB="research_restore_check"
if [ -z "$BACKUP_DIR" ]; then
  echo "先设 BACKUP_DIR。" >&2
  exit 2
fi

compose() {
  docker compose --project-directory "$REPO" -f "$REPO/docker-compose.yml" "$@"
}
log() {
  echo "$(date '+%F %T') $*" | tee -a "$BACKUP_DIR/restore-check.log"
}

latest="$(ls -1 "$BACKUP_DIR"/db/research-*.dump 2>/dev/null | sort | tail -n 1 || true)"
if [ -z "$latest" ]; then
  log "试恢复失败：$BACKUP_DIR/db 里没有导出"
  exit 1
fi

cleanup() {
  compose exec -T db dropdb -U research --if-exists "$CHECK_DB" > /dev/null 2>&1 || true
}
trap cleanup EXIT

cleanup
compose exec -T db createdb -U research "$CHECK_DB"
# 和正式库一样的分词设置，恢复时重算的全文索引才和正式库一致。
for setting in punctuation_ignore seg_with_duality multi_short; do
  compose exec -T db psql -U research -d "$CHECK_DB" -qc "ALTER DATABASE $CHECK_DB SET zhparser.$setting = on" > /dev/null
done
compose exec -T db pg_restore -U research -d "$CHECK_DB" --no-owner --exit-on-error < "$latest"

restored="$(compose exec -T db psql -U research -d "$CHECK_DB" -Atc 'SELECT count(*) FROM articles')"
expected="$(cat "$latest.articles" 2>/dev/null || echo 0)"
if [ "$restored" -lt "$expected" ]; then
  log "试恢复失败：$latest 恢复出 $restored 篇，导出时有 $expected 篇"
  exit 1
fi
log "试恢复通过：$latest 恢复出 $restored 篇（导出时 $expected 篇）"
