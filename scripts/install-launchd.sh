#!/bin/bash
# 在 Mac mini 上装定时任务（当前用户的 launchd）：
#   每晚 00:37 夜间补齐（外链图片、观点、摘要、论点；NIGHTLY_OCR=1 时再加图片文字）、
#   每晚 03:17 备份、每月 1 号 04:23 试恢复、
#   每天 08:10 / 20:10 方伟公众号抓取、08:40 / 20:40 方伟星球抓取（抓完入库，见 scripts/crawl-*.sh）。
#   每小时 AIHOT 增量导入默认不装（AIHOT 已下线；需要时设 INSTALL_INGEST_AIHOT=1）。
#   不想要夜间补齐时设 INSTALL_NIGHTLY=0；不想要定时抓取时设 INSTALL_CRAWL=0。
#
#   BACKUP_DIR=~/research-backups scripts/install-launchd.sh
#
# 卸载：scripts/install-launchd.sh --uninstall
set -euo pipefail

REPO="${REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
AGENTS="$HOME/Library/LaunchAgents"
LOGS="$HOME/Library/Logs/research"
NAMES="backup restore-check"
if [ "${INSTALL_NIGHTLY:-1}" = "1" ]; then
  NAMES="$NAMES nightly"
fi
if [ "${INSTALL_CRAWL:-1}" = "1" ]; then
  NAMES="$NAMES crawl-fangwei-mp crawl-fangwei-zsxq"
fi
if [ "${INSTALL_INGEST_AIHOT:-0}" = "1" ]; then
  NAMES="$NAMES ingest-aihot"
fi
DOMAIN="gui/$(id -u)"

if [ "${1:-}" = "--uninstall" ]; then
  for name in backup restore-check nightly crawl-fangwei-mp crawl-fangwei-zsxq ingest-aihot; do
    plist="$AGENTS/com.zldong.research.$name.plist"
    launchctl bootout "$DOMAIN" "$plist" 2>/dev/null || true
    rm -f "$plist"
    echo "已卸载 com.zldong.research.${name}"
  done
  # 定时抓取、导入和夜间补齐卸了，就不该再因为它们「太久没跑」提示。备份的状态留着：没有备份本身就该提示。
  rm -f "$REPO/data/status/ingest-aihot.json" "$REPO/data/status/nightly.json" \
    "$REPO/data/status/crawl-fangwei-mp.json" "$REPO/data/status/crawl-fangwei-zsxq.json"
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
    -e "s|__PATH__|$search_path|g" -e "s|__NIGHTLY_OCR__|${NIGHTLY_OCR:-0}|g" "$template" > "$plist"
  plutil -lint "$plist" > /dev/null
  launchctl bootout "$DOMAIN" "$plist" 2>/dev/null || true
  launchctl bootstrap "$DOMAIN" "$plist"
  echo "已装好 com.zldong.research.${name}"
done
if [ "${INSTALL_INGEST_AIHOT:-0}" != "1" ]; then
  # 以前装过、现在不装的，卸掉并删状态文件，免得首页一直提示「AIHOT 定时导入太久没成功」。
  old="$AGENTS/com.zldong.research.ingest-aihot.plist"
  if [ -f "$old" ]; then
    launchctl bootout "$DOMAIN" "$old" 2>/dev/null || true
    rm -f "$old" "$REPO/data/status/ingest-aihot.json"
    echo "已卸掉旧的 com.zldong.research.ingest-aihot（AIHOT 已下线）"
  fi
fi
echo "日志在 ${LOGS}"
