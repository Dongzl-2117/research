#!/bin/bash
# 每晚备份投资研究库：数据库导出一份，图片增量同步。数据库导出保留 KEEP_DAYS 天（默认 30）。
#
#   BACKUP_DIR=/Volumes/备份盘/research scripts/backup.sh
#
# BACKUP_DIR 放另一块盘或云盘同步目录，不要和仓库、Docker 卷在同一块盘上。
# 定时跑见 docs/backup.md（launchd）。结果写进 data/status/backup.json，失败或太久没成功时首页提示。
set -Eeuo pipefail

REPO="${REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
# 在任何容器命令前登记；持锁直到整次脚本退出，消除检查后的竞争空档。
if [ "${RESEARCH_TASK_LOCKED:-0}" != "1" ]; then
  export REPO
  exec python3 "$REPO/scripts/with_task_lock.py" /bin/bash "$0" "$@"
fi
BACKUP_DIR="${BACKUP_DIR:-}"
KEEP_DAYS="${KEEP_DAYS:-30}"
TASK=backup
. "$REPO/scripts/status.sh"
partial=""
trap 'STATUS_FAILED_LINE=$LINENO; STATUS_FAILED_CMD=$BASH_COMMAND' ERR
trap 'rc=$?; [ -n "$partial" ] && rm -f "$partial" "${partial}.count"; status_finish "$rc"' EXIT

if [ -z "$BACKUP_DIR" ]; then
  echo "先设 BACKUP_DIR：另一块盘或云盘同步目录。" >&2
  status_fail "没设 BACKUP_DIR"
  exit 2
fi

compose() {
  docker compose --project-directory "$REPO" -f "$REPO/docker-compose.yml" "$@"
}

stamp="$(date +%Y%m%d-%H%M%S)"
run_step "建备份目录" mkdir -p "$BACKUP_DIR/db" "$BACKUP_DIR/images"
partial="$BACKUP_DIR/db/.research-$stamp.dump.partial"
final="$BACKUP_DIR/db/research-$stamp.dump"

# 导出前记下篇数，每月试恢复时拿来核对。
run_step "读篇数" compose exec -T db psql -U research -d research -Atc 'SELECT count(*) FROM articles' \
  > "${partial}.count"
articles="$(tr -d '[:space:]' < "${partial}.count")"
run_step "导出数据库" compose exec -T db pg_dump -U research -d research --format=custom --compress=6 > "$partial"
# 导出能被 pg_restore 读出目录，才算这份是完整的。
run_step "核对导出" compose exec -T db pg_restore --list < "$partial" > /dev/null
mv "$partial" "$final"
rm -f "${partial}.count"
partial=""
echo "$articles" > "${final}.articles"

if [ -d "$REPO/data/images" ]; then
  # 图片按内容哈希存，同名就是同一张。只增不删，误删了也还在备份里。
  run_step "同步图片" rsync -a "$REPO/data/images/" "$BACKUP_DIR/images/"
fi

find "$BACKUP_DIR/db" -name 'research-*.dump*' -type f -mtime +"$KEEP_DAYS" -delete
size="$(du -h "$final" | cut -f1)"
echo "$(date '+%F %T') 备份完成：${final}，${size}，${articles} 篇" | tee -a "$BACKUP_DIR/backup.log"
status_ok "${size}，${articles} 篇"
