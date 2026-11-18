#!/bin/bash
# 晚点LatePost 每天抓两次（launchd 08:30、20:30，香港时间）：容器里的 research-crawl-wechat 向极致了查最新一页
# （每次约 0.14 元），新文章的正文从公众号原文页取（取不到才用极致了的正文，每篇约 0.03 元），写进
# data/inbox/latepost 并导入（--origin crawl）；再下载外链图片、给这一来源补摘要。
# 只查最新一页，不翻历史。全量回填是 research-crawl-wechat --source latepost --list-only / --backfill，
# 不在这个脚本里，部署和定时任务都不会跑。见 docs/ingest.md「晚点LatePost」。
# 极致了 key 和方伟公众号、孤独进化体共用：仓库根 .env 的 DAJIALA_KEY，docker compose 透传进容器。
#   scripts/crawl-latepost.sh
#   MAX_NEW=10 scripts/crawl-latepost.sh
#   DIGEST_LIMIT=0 scripts/crawl-latepost.sh   # 只抓取导入，不写摘要
set -Eeuo pipefail

REPO="${REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
SOURCE=latepost
MAX_NEW="${MAX_NEW:-10}"
DIGEST_LIMIT="${DIGEST_LIMIT:-10}"
TASK=crawl-latepost
. "$REPO/scripts/status.sh"
trap 'STATUS_FAILED_LINE=$LINENO; STATUS_FAILED_CMD=$BASH_COMMAND' ERR
trap 'rc=$?; status_finish "$rc"' EXIT

compose() {
  docker compose --project-directory "$REPO" -f "$REPO/docker-compose.yml" "$@"
}

echo "$(date '+%F %T') 开始晚点LatePost抓取（这次最多 ${MAX_NEW} 篇新文章）"
run_step "抓取并导入" compose_exec app research-crawl-wechat --source "$SOURCE" --max-new "$MAX_NEW"

echo "$(date '+%F %T') 下载外链图片"
run_step "下载外链图片" compose_exec app research-fetch-images --limit 200

# 只补这一来源最近没有摘要的，上限和这次新文章的篇数一样。历史回填导入之后，这里每次也只会写这么多篇，
# 不会把几千篇一次写完；设成 0 就先不写。全库夜间任务另有每晚的上限。
if [ "$DIGEST_LIMIT" -gt 0 ] 2>/dev/null; then
  echo "$(date '+%F %T') 补摘要（最多 ${DIGEST_LIMIT} 篇）"
  run_step "摘要" compose_exec app research-digest --source "$SOURCE" --limit "$DIGEST_LIMIT"
else
  echo "$(date '+%F %T') 跳过摘要（DIGEST_LIMIT=${DIGEST_LIMIT}）"
fi

status_ok "晚点LatePost抓取完成"
