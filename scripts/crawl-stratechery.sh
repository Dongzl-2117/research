#!/bin/bash
# Stratechery 白天也抓一次（launchd 21:15，香港时间）：读订阅自带的个人 RSS（.env 的 STRATECHERY_RSS_URL，不花钱），
# 库里没有的写归档、导入；再下载外链图片，新文章当场译制、整理判断、写摘要、给段落打分
# （research-process-new，不等夜间、不避开高峰价）。Thompson 北京时间傍晚发文，以前要等 00:37 的夜间任务才进库
# （Zhilin 11-20：白天再加一次）。夜间任务开头照旧抓一次，补这之后发的。
#   scripts/crawl-stratechery.sh
#   PROCESS_NEW=0 scripts/crawl-stratechery.sh   # 只抓取导入，新文章留给夜间
set -Eeuo pipefail

REPO="${REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
SOURCE=stratechery
TASK=crawl-stratechery
. "$REPO/scripts/status.sh"
trap 'STATUS_FAILED_LINE=$LINENO; STATUS_FAILED_CMD=$BASH_COMMAND' ERR
trap 'rc=$?; status_finish "$rc"' EXIT

compose() {
  docker compose --project-directory "$REPO" -f "$REPO/docker-compose.yml" "$@"
}

# 没设地址时跳过、不算失败（和夜间那一步一样）；设了却读不到（地址失效、订阅过期）research-crawl-stratechery 退出码 1，记成失败。
if ! compose_exec app sh -c 'test -n "${STRATECHERY_RSS_URL:-}"' 2>/dev/null; then
  echo "$(date '+%F %T') 跳过：.env 没设 STRATECHERY_RSS_URL"
  status_ok "跳过：.env 没设 STRATECHERY_RSS_URL"
  exit 0
fi

echo "$(date '+%F %T') 开始 Stratechery 抓取"
mark="$(new_mark)"
run_step "抓取并导入" compose_exec app research-crawl-stratechery

echo "$(date '+%F %T') 下载外链图片"
run_step "下载外链图片" compose_exec app research-fetch-images --limit 200

echo "$(date '+%F %T') 处理新文章"
run_step "处理新文章" process_new "$SOURCE" "$mark"

status_ok "Stratechery 抓取完成"
