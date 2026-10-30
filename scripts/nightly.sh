#!/bin/bash
# 夜间补齐：先跑还没跑过的一次性数据修复，再给外链图片、语义向量、观点、摘要、论点各补一批，可选图片文字（OCR）。
# 每一步有篇数上限，观点和摘要还有时间上限，跑不完的下一晚接着补。白天和部署时都不跑。
# 某一步失败不影响后面的步骤；结果写进 data/status/nightly.json，有步骤失败或两天没跑成时 research-status 会列出来。
#
#   scripts/nightly.sh
#   VIEWS_LIMIT=200 DIGEST_LIMIT=200 MINUTES=300 scripts/nightly.sh
#   NIGHTLY_OCR=1 scripts/nightly.sh        # 在 app 容器里走站内 deepseek-flash 识图
#   THESES_ENABLED=1 scripts/nightly.sh     # 夜间也补论点（默认关）
set -Euo pipefail

REPO="${REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
IMAGES_LIMIT="${IMAGES_LIMIT:-300}"
VIEWS_LIMIT="${VIEWS_LIMIT:-60}"
DIGEST_LIMIT="${DIGEST_LIMIT:-80}"
OCR_LIMIT="${OCR_LIMIT:-400}"
NIGHTLY_OCR="${NIGHTLY_OCR:-0}"
# 语义检索的向量：每晚最多补这么多块（没配 EMBEDDING_BASE_URL 时这一步什么都不做）。
EMBED_LIMIT="${EMBED_LIMIT:-50000}"
# 论点默认关。打开后补的是库里观点够数的公司，不限持仓。需要夜间跑时设 THESES_ENABLED=1。
THESES_ENABLED="${THESES_ENABLED:-0}"
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
  return "$rc"
}

# 一次性数据修复：改了导入或整理规则以后，库里已有的行要按新规则重算一遍。云端的 AI 连不上 Mac mini，
# 这类命令不再等人手动跑，写在下面由夜间任务跑。只放不调 LLM、能重复跑的命令（再跑一遍不改已经改好的行）。
# 成功跑完一次在 data/status/once/ 下留一个同名标记，以后的夜里跳过；失败的不留标记，下一晚再试。
# 新加一条接在最后，名字用「日期-做什么」；已经跑过的名字不要改，改了会再跑一遍。
ONCE_DIR="${ONCE_DIR:-$REPO/data/status/once}"
once_done=""
once() {
  local name="$1"
  shift
  if [ -f "$ONCE_DIR/$name" ]; then
    return 0
  fi
  if step "一次性修复 ${name}" "$@"; then
    mkdir -p "$ONCE_DIR" && date '+%F %T' > "$ONCE_DIR/$name"
    once_done="${once_done}${once_done:+、}${name}"
  fi
  return 0
}

# 乘风破浪的发布时间从当天零点改成归档头里的钟点（#130）。
once 2026-10-29-retime-chengfengpolang compose_exec app research-retime --source chengfengpolang
# 乘风破浪的标题写回库（#128），评论区只留星主（#151），星主在楼中楼里的回答留着（#155）。
once 2026-10-30-repair-chengfengpolang compose_exec app research-repair-zsxq --source chengfengpolang
# 星球长文帖的检索文字和段落块去掉导出脚手架（#124）。
once 2026-10-29-clean-zsxq compose_exec app research-clean --source chengfengpolang --source fangwei-zsxq
# 检索里的图片文字块按现在的整理规则重切：去掉优惠券卡、入群提示和群友昵称、状态栏（#147）。只读已识别的字，不识图。
once 2026-10-30-rebuild-ocr-chunks compose_exec app research-rebuild-chunks --ocr

step "外链图片" compose_exec app research-fetch-images --limit "$IMAGES_LIMIT"
step "语义向量" compose_exec app research-embed --limit "$EMBED_LIMIT"
step "观点" compose_exec app research-views --limit "$VIEWS_LIMIT" --max-minutes "$MINUTES"
step "摘要" compose_exec app research-digest --limit "$DIGEST_LIMIT" --max-minutes "$MINUTES"
if [ "$THESES_ENABLED" = "1" ]; then
  step "论点" compose_exec app research-theses
fi
if [ "$NIGHTLY_OCR" = "1" ]; then
  step "图片文字" compose_exec app research-ocr --engine vision --limit "$OCR_LIMIT" --max-minutes "$MINUTES"
fi

if [ -n "$failed" ]; then
  status_fail "失败的步骤：${failed}"
  exit 1
fi
parts="外链图片、语义向量、观点、摘要"
[ -n "$once_done" ] && parts="一次性修复 ${once_done}；${parts}"
[ "$THESES_ENABLED" = "1" ] && parts="${parts}、论点"
[ "$NIGHTLY_OCR" = "1" ] && parts="${parts}、图片文字"
status_ok "$parts"
