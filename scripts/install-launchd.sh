#!/bin/bash
# 在 Mac mini 上装定时任务（当前用户的 launchd）：
#   每晚 03:17 备份、每月 1 号 04:23 试恢复。
#   每小时 AIHOT 增量导入默认不装（AIHOT 抓取停着；需要时设 INSTALL_INGEST_AIHOT=1）。
#
#   BACKUP_DIR=~/research-backups scripts/install-launchd.sh
#
# 卸载：scripts/install-launchd.sh --uninstall
set -euo pipefail

REPO="${REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
AGENTS="$HOME/Library/LaunchAgents"
LOGS="$HOME/Library/Logs/research"
NAMES="backup restore-check"
if [ "${INSTALL_INGEST_AIHOT:-0}" = "1" ]; then
  NAMES="$NAMES ingest-aihot"
fi
DOMAIN="gui/$(id -u)"

if [ "${1:-}" = "--uninstall" ]; then
  for name in backup restore-check ingest-aihot; do
    plist="$AGENTS/com.zldong.research.$name.plist"
    launchctl bootout "$DOMAIN" "$plist" 2>/dev/null || true
    rm -f "$plist"
    echo "已卸载 com.zldong.research.$name"
  done
  exit 0
fi

BACKUP_DIR="${BACKUP_DIR:-}"
if [ -z "$BACKUP_DIR" ]; then
  echo "先设 BACKUP_DIR：例如 ~/research-backups（同盘）或另一块盘/云盘同步目录。" >&2
  exit 2
fi
docker_bin="$(command -v docker || true)"
if [ -z "$docker_bin" ]; then
  echo "找不到 docker 命令。" >&2
  exit 2
fi
search_path="$(dirname "$docker_bin"):/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

mkdir -p "$AGENTS" "$LOGS" "$BACKUP_DIR"
for name in $NAMES; do
  template="$REPO/deploy/launchd/com.zldong.research.$name.plist"
  plist="$AGENTS/com.zldong.research.$name.plist"
  sed -e "s|__REPO__|$REPO|g" -e "s|__BACKUP_DIR__|$BACKUP_DIR|g" -e "s|__LOGS__|$LOGS|g" \
    -e "s|__PATH__|$search_path|g" "$template" > "$plist"
  plutil -lint "$plist" > /dev/null
  launchctl bootout "$DOMAIN" "$plist" 2>/dev/null || true
  launchctl bootstrap "$DOMAIN" "$plist"
  echo "已装好 com.zldong.research.$name"
done
if [ "${INSTALL_INGEST_AIHOT:-0}" != "1" ]; then
  echo "未装 ingest-aihot（AIHOT 抓取停着；需要时 INSTALL_INGEST_AIHOT=1 再跑一次本脚本）"
fi
echo "日志在 $LOGS"
