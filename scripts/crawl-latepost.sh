#!/bin/bash
# 晚点LatePost 每天抓两次（launchd 08:30、20:30，香港时间）：容器里的 research-crawl-wechat 向极致了查最新一页
# （每次约 0.14 元），新文章的正文从公众号原文页取（取不到才用极致了的正文，每篇约 0.03 元），写进
# data/inbox/latepost 并导入（--origin crawl）；再下载外链图片，这次新进来的文章当场识图、整理判断、写摘要、
# 给段落打分（research-process-new，不等夜间、不避开高峰价）。
# 只查最新一页，不翻历史。全量回填是 research-crawl-wechat --source latepost --list-only / --backfill，
# 不在这个脚本里，部署和定时任务都不会跑。见 docs/ingest.md「晚点LatePost」。
# 极致了 key 和方伟公众号、孤独进化体共用：仓库根 .env 的 DAJIALA_KEY，docker compose 透传进容器。
#   scripts/crawl-latepost.sh
#   MAX_NEW=10 scripts/crawl-latepost.sh
#   PROCESS_NEW=0 scripts/crawl-latepost.sh    # 只抓取导入，新文章留给夜间
set -Eeuo pipefail

REPO="${REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
SOURCE=latepost
MAX_NEW="${MAX_NEW:-10}"
TASK=crawl-latepost
. "$REPO/scripts/status.sh"
trap 'STATUS_FAILED_LINE=$LINENO; STATUS_FAILED_CMD=$BASH_COMMAND' ERR
trap 'rc=$?; status_finish "$rc"' EXIT

compose() {
  docker compose --project-directory "$REPO" -f "$REPO/docker-compose.yml" "$@"
}

echo "$(date '+%F %T') 开始晚点LatePost抓取（这次最多 ${MAX_NEW} 篇新文章）"
mark="$(new_mark)"
run_step "抓取并导入" compose_exec app research-crawl-wechat --source "$SOURCE" --max-new "$MAX_NEW"

echo "$(date '+%F %T') 下载外链图片"
run_step "下载外链图片" compose_exec app research-fetch-images --limit 200

# 只处理这次抓进来的（id 比抓取前大的）。历史回填另由 research-ingest-wechat 导入，不经过这里，
# 导入的几千篇由夜间任务按每晚上限补，不会在这里一次做完。
echo "$(date '+%F %T') 处理新文章"
run_step "处理新文章" process_new "$SOURCE" "$mark"

status_ok "晚点LatePost抓取完成"
