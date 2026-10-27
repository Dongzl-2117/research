#!/bin/bash
# 夜间补齐：外链图片、观点、摘要、论点各补一批，可选图片文字（OCR）。每一步有篇数上限，
# 观点和摘要还有时间上限，跑不完的下一晚接着补。白天和部署时都不跑。
# 某一步失败不影响后面的步骤；结果写进 data/status/nightly.json，有步骤失败或两天没跑成时首页提示。
#
#   scripts/nightly.sh
#   VIEWS_LIMIT=200 DIGEST_LIMIT=200 MINUTES=300 scripts/nightly.sh
#   VIEWS_WORKERS=1 scripts/nightly.sh      # 观点一篇一篇抽（默认同时 3 篇）
#   VIEWS_EFFORT=high scripts/nightly.sh    # 观点这一步用 high 推理（默认跟 .env，DeepSeek 是 max）
#   NIGHTLY_OCR=1 scripts/nightly.sh        # 先 docker compose --profile ocr build ocr
#   THESES_SCOPE=all scripts/nightly.sh     # 论点补全部观点够数的公司（默认只补持仓和观察）
#   THESES_SCOPE=off scripts/nightly.sh     # 不补论点
set -Euo pipefail

REPO="${REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
IMAGES_LIMIT="${IMAGES_LIMIT:-300}"
VIEWS_LIMIT="${VIEWS_LIMIT:-60}"
# 观点同时抽几篇。篇数上限还是 VIEWS_LIMIT，只是跑得快：一篇一篇来时 MINUTES 分钟常常不够六十篇。
VIEWS_WORKERS="${VIEWS_WORKERS:-3}"
# 观点这一步的推理强度（low、high、max）。空着就跟 .env 的 DEEPSEEK_REASONING_EFFORT。
VIEWS_EFFORT="${VIEWS_EFFORT:-}"
DIGEST_LIMIT="${DIGEST_LIMIT:-80}"
OCR_LIMIT="${OCR_LIMIT:-400}"
NIGHTLY_OCR="${NIGHTLY_OCR:-0}"
# 语义检索的向量：每晚最多补这么多块（没配 EMBEDDING_BASE_URL 时这一步什么都不做）。
EMBED_LIMIT="${EMBED_LIMIT:-50000}"
# 论点：portfolio 只补持仓和观察名单（config/portfolio.yaml，现在 6 家）里观点够数的公司。观点有变化才重算、
# 一家一天最多一次，所以每晚最多 6 次模型调用。all 补全部观点够数的公司，off 不跑。
# 以前的开关还认：THESES_ENABLED=1 等于 all，THESES_ENABLED=0 等于 off。
THESES_SCOPE="${THESES_SCOPE:-portfolio}"
case "${THESES_ENABLED:-}" in
  1) THESES_SCOPE=all ;;
  0) THESES_SCOPE=off ;;
esac
# 观点和摘要各自最多跑这么多分钟（xhigh 推理一篇要一两分钟）。
MINUTES="${MINUTES:-150}"
TASK=nightly
. "$REPO/scripts/status.sh"
trap 'rc=$?; status_finish "$rc"' EXIT

compose() {
  docker compose --project-directory "$REPO" -f "$REPO/docker-compose.yml" "$@"
}

failed=""
step() {
  # step 名字 命令…：跑一步，失败只记下来（带错误输出的最后一行），接着跑下一步。
  local name="$1"
  shift
  local err rc last
  err="$(mktemp "${TMPDIR:-/tmp}/research-nightly.XXXXXX")"
  echo "$(date '+%F %T') 开始：${name}"
  if "$@" 2> "$err"; then
    cat "$err" >&2
    rm -f "$err"
    echo "$(date '+%F %T') 完成：${name}"
    return 0
  else
    rc=$?
  fi
  cat "$err" >&2
  last="$(grep -v '^[[:space:]]*$' "$err" | tail -n 1 || true)"
  rm -f "$err"
  echo "$(date '+%F %T') 失败：${name}（退出码 ${rc}）" >&2
  failed="${failed}${failed:+；}${name}（退出码 ${rc}${last:+：}${last}）"
}

step "外链图片" compose exec -T app research-fetch-images --limit "$IMAGES_LIMIT"
step "语义向量" compose exec -T app research-embed --limit "$EMBED_LIMIT"
if [ -n "$VIEWS_EFFORT" ]; then
  step "观点" compose exec -T -e "DEEPSEEK_REASONING_EFFORT=${VIEWS_EFFORT}" app \
    research-views --limit "$VIEWS_LIMIT" --max-minutes "$MINUTES" --workers "$VIEWS_WORKERS"
else
  step "观点" compose exec -T app \
    research-views --limit "$VIEWS_LIMIT" --max-minutes "$MINUTES" --workers "$VIEWS_WORKERS"
fi
step "摘要" compose exec -T app research-digest --limit "$DIGEST_LIMIT" --max-minutes "$MINUTES"
case "$THESES_SCOPE" in
  portfolio) step "论点" compose exec -T app research-theses --portfolio ;;
  all) step "论点" compose exec -T app research-theses ;;
  off) ;;
  *) failed="${failed}${failed:+；}论点（THESES_SCOPE=${THESES_SCOPE} 不认识，只能是 portfolio、all、off）" ;;
esac
if [ "$NIGHTLY_OCR" = "1" ]; then
  step "图片文字" compose --profile ocr run --rm -T ocr research-ocr --limit "$OCR_LIMIT" --max-minutes "$MINUTES"
fi

if [ -n "$failed" ]; then
  status_fail "失败的步骤：${failed}"
  exit 1
fi
parts="外链图片、语义向量、观点、摘要"
case "$THESES_SCOPE" in
  portfolio) parts="${parts}、论点（持仓和观察）" ;;
  all) parts="${parts}、论点" ;;
esac
[ "$NIGHTLY_OCR" = "1" ] && parts="${parts}、图片文字"
status_ok "$parts"
