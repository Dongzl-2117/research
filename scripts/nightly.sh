#!/bin/bash
# 夜间补齐：外链图片、观点、摘要、论点各补一批，可选图片文字（OCR）。每一步有篇数上限，
# 观点和摘要还有时间上限，跑不完的下一晚接着补。白天和部署时都不跑。
# 某一步失败不影响后面的步骤；结果写进 data/status/nightly.json，有步骤失败或两天没跑成时首页提示。
#
#   scripts/nightly.sh
#   VIEWS_LIMIT=200 DIGEST_LIMIT=200 MINUTES=300 scripts/nightly.sh
#   NIGHTLY_OCR=1 scripts/nightly.sh        # 先 docker compose --profile ocr build ocr
set -Euo pipefail

REPO="${REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
IMAGES_LIMIT="${IMAGES_LIMIT:-300}"
VIEWS_LIMIT="${VIEWS_LIMIT:-60}"
DIGEST_LIMIT="${DIGEST_LIMIT:-80}"
OCR_LIMIT="${OCR_LIMIT:-400}"
NIGHTLY_OCR="${NIGHTLY_OCR:-0}"
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
step "观点" compose exec -T app research-views --limit "$VIEWS_LIMIT" --max-minutes "$MINUTES"
step "摘要" compose exec -T app research-digest --limit "$DIGEST_LIMIT" --max-minutes "$MINUTES"
step "论点" compose exec -T app research-theses
if [ "$NIGHTLY_OCR" = "1" ]; then
  step "图片文字" compose --profile ocr run --rm -T ocr research-ocr --limit "$OCR_LIMIT" --max-minutes "$MINUTES"
fi

if [ -n "$failed" ]; then
  status_fail "失败的步骤：${failed}"
  exit 1
fi
status_ok "外链图片、观点、摘要、论点$([ "$NIGHTLY_OCR" = "1" ] && echo '、图片文字')"
