#!/bin/bash
# 每晚备份投资研究库：数据库导出一份，图片增量同步。数据库导出保留 KEEP_DAYS 天（默认 7）。
#
#   BACKUP_DIR=/Volumes/备份盘/research scripts/backup.sh
#
# BACKUP_DIR 放另一块盘或云盘同步目录，不要和仓库、Docker 卷在同一块盘上。
# 定时跑见 docs/backup.md（launchd）。结果写进 data/status/backup.json，失败或太久没成功时 docker compose exec app research-status 会报出来。
set -Eeuo pipefail

REPO="${REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
BACKUP_DIR="${BACKUP_DIR:-}"
KEEP_DAYS="${KEEP_DAYS:-7}"
TASK=backup
. "$REPO/scripts/status.sh"
staging=""
trap 'STATUS_FAILED_LINE=$LINENO; STATUS_FAILED_CMD=$BASH_COMMAND' ERR
cleanup() {
  if [ -n "$staging" ]; then
    rm -f "$staging/archive" "$staging/articles" "$staging/has-images"
    rmdir "$staging"
  fi
}
trap 'rc=$?; cleanup; status_finish "$rc"' EXIT

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
run_step "核对备份盘可用空间" python3 "$REPO/scripts/backup_space.py" "$BACKUP_DIR/db" "${BACKUP_EXPECTED_MIB:-}"
staging="$(mktemp -d "$BACKUP_DIR/db/.research-${stamp}-v2-XXXXXX")"
name="$(basename "$staging")"
final="$BACKUP_DIR/db/${name#.}.dump"

dump_archive() {
  # psql 持有只读事务直至子进程 pg_dump 结束。第一行是同一快照的篇数和图片引用标记，其余字节是原始 custom dump。
  compose exec -T db psql -XqAt -v ON_ERROR_STOP=1 -U research -d research <<'SQL' | {
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY;
\copy (SELECT pg_export_snapshot(), count(*), EXISTS (SELECT 1 FROM article_images) FROM articles) TO PROGRAM 'read -r snap n images && printf "%s %s\n" "$n" "$images" && exec pg_dump -U research -d research --format=custom --compress=6 --snapshot="$snap"'
COMMIT;
SQL
    IFS=" " read -r articles images || return 1
    case "$images" in t | f) ;; *) return 1 ;; esac
    case "$articles" in '' | *[!0-9]*) return 1 ;; esac
    printf '%s\n' "$articles" > "$staging/articles" && printf '%s\n' "$images" > "$staging/has-images" && cat > "$staging/archive"
  }
}

run_step "导出数据库并记下快照篇数" dump_archive
articles="$(cat "$staging/articles")"
run_step "核对导出目录" compose exec -T db pg_restore --list < "$staging/archive" > /dev/null

if [ -d "$REPO/data/images" ]; then
  # 图片按内容哈希存，同名就是同一张。只增不删，误删了也还在备份里。
  run_step "同步图片" rsync -a "$REPO/data/images/" "$BACKUP_DIR/images/"
elif [ "$(cat "$staging/has-images")" = t ]; then
  echo "导出中有图片引用，但找不到原图目录 $REPO/data/images。" >&2
  status_fail "导出中有图片引用，找不到用于同步的原图目录"
  exit 1
fi

# 恢复只会选中 .dump。图片同步及篇数记录就绪后才公布导出，正在运行或失败的备份不会盖过上一份。
mv "$staging/articles" "${final}.articles"
mv "$staging/archive" "$final"
find "$BACKUP_DIR/db" -name 'research-*.dump*' -type f -mtime +"$KEEP_DAYS" -delete
size="$(du -h "$final" | cut -f1)"
echo "$(date '+%F %T') 备份完成：${final}，${size}，${articles} 篇" | tee -a "$BACKUP_DIR/backup.log"
# 清理过期文件后再看目的盘。已成功的快照照常公布，余量不足以再写一份时另报提醒。
warning=""
if ! warning="$(python3 "$REPO/scripts/backup_space.py" "$BACKUP_DIR/db" 2>&1)"; then
  echo "备份完成时：${warning}" >&2
fi
status_ok "${size}，${articles} 篇" "$warning"
