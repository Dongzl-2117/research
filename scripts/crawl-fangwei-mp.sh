#!/bin/bash
# 方伟公众号每天抓两次（launchd 08:10、20:10）：容器里的 research-crawl-wechat 向极致了查最新文章（每次约 0.14 元），
# 新文章的正文从公众号原文页取，图就在原文的位置（取不到才用极致了的正文，每篇约 0.03 元），写进
# data/inbox/fangwei-mp 并导入（--origin crawl）；再下载外链图片、给新文章补摘要。和孤独进化体同一条路。
# 极致了 key 是仓库根 .env 的 DAJIALA_KEY，docker compose 透传进容器。
# 11 月 7 日以前用的 data/inbox/fangwei-mp/dl.py（不在仓库里）取的是极致了按段估图位置的正文，现在不再调用；
# 它写下的归档留在 data/inbox/fangwei-mp，research-crawl-wechat 每次照旧把整个目录重新导入。
#   scripts/crawl-fangwei-mp.sh
#   MAX_NEW=10 scripts/crawl-fangwei-mp.sh
set -Eeuo pipefail

REPO="${REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
SOURCE=fangwei-mp
MAX_NEW="${MAX_NEW:-5}"
TASK=crawl-fangwei-mp
. "$REPO/scripts/status.sh"
trap 'STATUS_FAILED_LINE=$LINENO; STATUS_FAILED_CMD=$BASH_COMMAND' ERR
trap 'rc=$?; status_finish "$rc"' EXIT

compose() {
  docker compose --project-directory "$REPO" -f "$REPO/docker-compose.yml" "$@"
}

echo "$(date '+%F %T') 开始方伟公众号抓取（这次最多 ${MAX_NEW} 篇新文章）"
run_step "抓取并导入" compose_exec app research-crawl-wechat --source "$SOURCE" --max-new "$MAX_NEW"

echo "$(date '+%F %T') 下载外链图片"
run_step "下载外链图片" compose_exec app research-fetch-images --limit 200

# 给最近没有摘要的方伟公众号补摘要（上限 20，避免拖太久）
echo "$(date '+%F %T') 补摘要"
run_step "摘要" compose_exec app research-digest --source "$SOURCE" --limit 20

status_ok "方伟公众号抓取完成"
