#!/bin/bash
# 在 Mac mini 上装定时任务（当前用户的 launchd）：
#   每晚 00:37 夜间补齐（外链图片、观点、摘要、论点；NIGHTLY_OCR=1 时再加图片文字）、
#   每晚 03:17 备份、每月 1 号 04:23 试恢复、
#   每天 08:10 / 20:10 方伟公众号抓取、08:40 / 20:40 方伟星球抓取、09:20 孤独进化体抓取（抓完入库，见 scripts/crawl-*.sh）、
#   每 10 分钟自动上线（main 上 CI 通过的新提交拉下来重建，见 scripts/deploy.sh；装好时先跑一次）。
#   每小时 AIHOT 增量导入默认不装（AIHOT 已下线；需要时设 INSTALL_INGEST_AIHOT=1）。
#   不想要夜间补齐时设 INSTALL_NIGHTLY=0；不想要定时抓取时设 INSTALL_CRAWL=0；不想自动上线时设 INSTALL_DEPLOY=0。
#
#   BACKUP_DIR=~/research-backups scripts/install-launchd.sh
#
# 这些设置记在 data/launchd.env，重跑时不用再写（命令行上设了的优先）。还没记下设置的，
# BACKUP_DIR 和 NIGHTLY_OCR 从以前装好的任务里读。
#
# 卸载：scripts/install-launchd.sh --uninstall
set -euo pipefail

REPO="${REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
AGENTS="$HOME/Library/LaunchAgents"
LOGS="$HOME/Library/Logs/research"
SETTINGS="$REPO/data/launchd.env"
if [ -f "$SETTINGS" ]; then
  . "$SETTINGS"
fi

installed() {
  # installed 任务 变量：以前装好的这个任务里设的环境变量，没装过或读不到就空。
  local plist="$AGENTS/com.zldong.research.$1.plist"
  if [ -f "$plist" ] && [ -x /usr/libexec/PlistBuddy ]; then
    /usr/libexec/PlistBuddy -c "Print :EnvironmentVariables:$2" "$plist" 2>/dev/null || true
  fi
}

INSTALL_NIGHTLY="${INSTALL_NIGHTLY:-${SAVED_INSTALL_NIGHTLY:-1}}"
INSTALL_CRAWL="${INSTALL_CRAWL:-${SAVED_INSTALL_CRAWL:-1}}"
INSTALL_INGEST_AIHOT="${INSTALL_INGEST_AIHOT:-${SAVED_INSTALL_INGEST_AIHOT:-0}}"
INSTALL_DEPLOY="${INSTALL_DEPLOY:-${SAVED_INSTALL_DEPLOY:-1}}"
NIGHTLY_OCR="${NIGHTLY_OCR:-${SAVED_NIGHTLY_OCR:-$(installed nightly NIGHTLY_OCR)}}"
NIGHTLY_OCR="${NIGHTLY_OCR:-0}"
NAMES="backup restore-check"
if [ "$INSTALL_NIGHTLY" = "1" ]; then
  NAMES="$NAMES nightly"
fi
if [ "$INSTALL_CRAWL" = "1" ]; then
  NAMES="$NAMES crawl-fangwei-mp crawl-fangwei-zsxq crawl-gudujinhuati"
fi
if [ "$INSTALL_INGEST_AIHOT" = "1" ]; then
  NAMES="$NAMES ingest-aihot"
fi
if [ "$INSTALL_DEPLOY" = "1" ]; then
  NAMES="$NAMES deploy"
fi
DOMAIN="gui/$(id -u)"

if [ "${1:-}" = "--uninstall" ]; then
  for name in backup restore-check nightly crawl-fangwei-mp crawl-fangwei-zsxq crawl-gudujinhuati ingest-aihot deploy; do
    plist="$AGENTS/com.zldong.research.$name.plist"
    launchctl bootout "$DOMAIN" "$plist" 2>/dev/null || true
    rm -f "$plist"
    echo "已卸载 com.zldong.research.${name}"
  done
  # 定时抓取、导入、夜间补齐和自动上线卸了，就不该再因为它们「太久没跑」或上次失败提示。备份的状态留着：没有备份本身就该提示。
  rm -f "$REPO/data/status/ingest-aihot.json" "$REPO/data/status/nightly.json" \
    "$REPO/data/status/crawl-fangwei-mp.json" "$REPO/data/status/crawl-fangwei-zsxq.json" \
    "$REPO/data/status/crawl-gudujinhuati.json" \
    "$REPO/data/status/deploy.json" "$REPO/data/status/.deploy-last" "$REPO/data/status/.deploy-seen"
  exit 0
fi

BACKUP_DIR="${BACKUP_DIR:-${SAVED_BACKUP_DIR:-$(installed backup BACKUP_DIR)}}"
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
    -e "s|__PATH__|$search_path|g" -e "s|__NIGHTLY_OCR__|${NIGHTLY_OCR}|g" "$template" > "$plist"
  plutil -lint "$plist" > /dev/null
  if [ "$name" = "deploy" ] && [ "${DEPLOY_SELF:-0}" = "1" ]; then
    # 自动上线时重装：这个任务正在跑，卸下来会把自己停掉。新设置写好了，下次登录或手动重装时生效。
    echo "com.zldong.research.deploy 正在跑，新设置下次装载时生效"
    continue
  fi
  launchctl bootout "$DOMAIN" "$plist" 2>/dev/null || true
  launchctl bootstrap "$DOMAIN" "$plist"
  echo "已装好 com.zldong.research.${name}"
done
if [ "$INSTALL_INGEST_AIHOT" != "1" ]; then
  # 以前装过、现在不装的，卸掉并删状态文件，免得状态里一直挂着「AIHOT 定时导入太久没成功」。
  old="$AGENTS/com.zldong.research.ingest-aihot.plist"
  if [ -f "$old" ]; then
    launchctl bootout "$DOMAIN" "$old" 2>/dev/null || true
    rm -f "$old" "$REPO/data/status/ingest-aihot.json"
    echo "已卸掉旧的 com.zldong.research.ingest-aihot（AIHOT 已下线）"
  fi
fi
if [ "$INSTALL_DEPLOY" != "1" ]; then
  old="$AGENTS/com.zldong.research.deploy.plist"
  if [ -f "$old" ]; then
    launchctl bootout "$DOMAIN" "$old" 2>/dev/null || true
    rm -f "$old" "$REPO/data/status/deploy.json" "$REPO/data/status/.deploy-last" "$REPO/data/status/.deploy-seen"
    echo "已卸掉 com.zldong.research.deploy（不自动上线了）"
  fi
fi
mkdir -p "$(dirname "$SETTINGS")"
{
  printf 'SAVED_BACKUP_DIR=%q\n' "$BACKUP_DIR"
  printf 'SAVED_INSTALL_NIGHTLY=%q\n' "$INSTALL_NIGHTLY"
  printf 'SAVED_INSTALL_CRAWL=%q\n' "$INSTALL_CRAWL"
  printf 'SAVED_INSTALL_INGEST_AIHOT=%q\n' "$INSTALL_INGEST_AIHOT"
  printf 'SAVED_INSTALL_DEPLOY=%q\n' "$INSTALL_DEPLOY"
  printf 'SAVED_NIGHTLY_OCR=%q\n' "$NIGHTLY_OCR"
} > "$SETTINGS"
echo "日志在 ${LOGS}"
