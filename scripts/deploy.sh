#!/bin/bash
# 自动上线：main 上有新提交，而且这个提交在 GitHub 上的 CI（Tests）通过了，就拉代码、重建并重启应用。
# 定时任务 com.zldong.research.deploy 每 10 分钟跑一次（scripts/install-launchd.sh 装），合并进 main 的改动
# 等 CI 跑完再等下一轮，一般 10 到 20 分钟上线。迁移和后台重算在应用启动时自己做。
#
#   scripts/deploy.sh              # 手动跑一次：有新提交并且 CI 过了就上线
#   FORCE=1 scripts/deploy.sh      # 不等 CI、不等别的定时任务，上次失败过的提交也重试
#
# 镜像里带着这次的提交号（GIT_SHA），/health 报出这个提交才算上线。重建失败，或者重启后等不到新提交，
# 仓库退回上线前的提交、按它重建，线上回到原来的版本。数据库不退：已经跑过的迁移留着，要退数据用迁移前的备份。
# 带新迁移（src/researchlib/migrations/ 有变化）的提交，上线前先跑一次 scripts/backup.sh，备份失败不上线。
# 上线成功后删掉换下来的旧镜像（只删这个项目建的）。
#
# 结果写进 data/status/deploy.json，首页只在失败时提示：拉代码、重建失败，重启后应用没起来，CI 没过，
# 仓库不在 main、有没提交的改动或者没推送的提交，或者 6 小时取不到 main、查不到 CI。偶尔断网的那一轮什么都不记，下一轮再试。
# 重建失败的提交最多试 3 轮；建好了却起不来的不自动重试（每试一次线上要停好几分钟）。两种都等下一个提交或 FORCE=1。
# CI 没过的提交每轮再查，CI 重跑通过了就上线。
# 夜间补齐、定时抓取、备份正在跑时先不重启：它们在容器里跑命令，重建会把它们打断。
# 定时任务的模板（deploy/launchd/、install-launchd.sh）改了，上线后按上次安装的设置重装一遍。
#
# 拉代码会改到这个脚本自己，所以全部写在函数里，bash 先读完整个文件再跑。
# 只用 macOS 自带 bash 3.2 也有的写法。变量后面紧跟中文时一律写成 ${变量}。
set -Eeuo pipefail

REPO="${REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
BRANCH="${DEPLOY_BRANCH:-main}"
CHECK_NAME="${DEPLOY_CHECK:-Tests}"
APP_URL="${APP_URL:-http://127.0.0.1:8088/health}"
DEPLOY_WAIT="${DEPLOY_WAIT:-600}"
MAX_TRIES=3
TASK=deploy
# 这个目录里的文件变了就是有新迁移，上线前先备份。
MIGRATIONS="src/researchlib/migrations"
# Dockerfile 给镜像打的标签。上线后只清理带这个标签的旧镜像，Mac mini 上别的项目的不碰。
IMAGE_LABEL="com.zldong.research"
# 重建时会被打断的定时任务（scripts/ 下的脚本名）。
JOBS="nightly crawl-fangwei-mp crawl-fangwei-zsxq ingest-aihot backup restore-check"
# launchd 里没有终端：git 要密码时直接失败，不要卡住等输入。
export GIT_TERMINAL_PROMPT=0
. "$REPO/scripts/status.sh"
LAST="$STATUS_DIR/.deploy-last"
SEEN="$STATUS_DIR/.deploy-seen"
LOCK="$STATUS_DIR/.deploy.lock"
locked=0
LAST_ERR=""
trap 'STATUS_FAILED_LINE=$LINENO; STATUS_FAILED_CMD=$BASH_COMMAND' ERR
trap 'rc=$?; if [ "$locked" = "1" ]; then rm -rf "$LOCK"; fi; status_finish "$rc"' EXIT

compose() {
  docker compose --project-directory "$REPO" -f "$REPO/docker-compose.yml" "$@"
}

note() {
  echo "$(date '+%F %T') $*"
}

attempt() {
  # attempt 命令…：和 run_step 一样跑一步，但失败时不退出，错误输出的最后一行放进 LAST_ERR，由调用的人收拾。
  local err rc=0
  err="$(mktemp "${TMPDIR:-/tmp}/research-deploy.XXXXXX")"
  "$@" 2> "$err" || rc=$?
  cat "$err" >&2
  LAST_ERR="$(grep -v '^[[:space:]]*$' "$err" | tail -n 1 || true)"
  rm -f "$err"
  return "$rc"
}

serving() {
  # serving [提交]：/health 通了算起来了；给了提交的，还要它报的就是这个提交（镜像里的 GIT_SHA），
  # 免得旧容器还在答话就当成上线了。
  local want="${1:-}" body
  body="$(curl -fsS --max-time 5 "$APP_URL" 2> /dev/null)" || return 1
  if [ -z "$want" ]; then
    return 0
  fi
  printf '%s' "$body" | grep -q "\"commit\": *\"${want}\""
}

wait_serving() {
  # wait_serving [提交]：最多等 DEPLOY_WAIT 秒。启动时要跑迁移，大的迁移要几分钟。
  local waited=0
  until serving "${1:-}"; do
    if [ "$waited" -ge "$DEPLOY_WAIT" ]; then
      return 1
    fi
    sleep 5
    waited=$((waited + 5))
  done
}

saved_backup_dir() {
  # install-launchd.sh 记下的备份目录（data/launchd.env 里的 SAVED_BACKUP_DIR），没记就空。
  local SAVED_BACKUP_DIR=""
  if [ -f "$REPO/data/launchd.env" ]; then
    . "$REPO/data/launchd.env"
  fi
  printf '%s' "$SAVED_BACKUP_DIR"
}

go_back() {
  # go_back 上线前的提交 原因 [提示]：仓库退回去、按它重建，线上和仓库对得上（夜间补齐这些用的是仓库里的脚本）。
  # 镜像没建成时旧容器一直在跑，这一步多半什么都不用换。数据库不退。
  local before="$1" reason="$2" hint="${3:-}" how
  note "${reason}，退回 ${before:0:7}"
  export GIT_SHA="$before"
  how="已退回 ${before:0:7}"
  if ! attempt git -C "$REPO" reset --quiet --keep "$before"; then
    how="仓库没能退回 ${before:0:7}（${LAST_ERR}）"
  elif ! attempt compose up -d --build; then
    how="${how}，重建旧版本也失败（${LAST_ERR}）"
  fi
  if wait_serving; then
    status_fail "${reason}；${how}，线上照常${hint:+。}${hint}"
  else
    status_fail "${reason}；${how}，应用还是没起来，线上停着：看 docker compose logs app"
  fi
}

unreachable() {
  # 取不到 main 或查不到 CI：偶尔一次不算失败。6 小时都没成过（或者装好以后从没成过）才记成失败。
  if [ -n "$(find "$SEEN" -mmin -360 2> /dev/null || true)" ]; then
    note "$1，下一轮再试"
  else
    status_fail "$1"
  fi
  exit 0
}

take_lock() {
  mkdir -p "$STATUS_DIR"
  if ! mkdir "$LOCK" 2> /dev/null; then
    local holder
    holder="$(cat "$LOCK/pid" 2> /dev/null || true)"
    if [ -n "$holder" ] && kill -0 "$holder" 2> /dev/null; then
      note "上一轮还在跑（进程 ${holder}），这轮跳过"
      exit 0
    fi
    rm -rf "$LOCK"
    mkdir "$LOCK"
  fi
  locked=1
  echo "$$" > "$LOCK/pid"
}

busy_job() {
  # 正在跑的定时任务，没有就什么都不打。
  local name
  for name in $JOBS; do
    if pgrep -f "scripts/${name}[.]sh" > /dev/null 2>&1; then
      echo "$name"
      return 0
    fi
  done
}

ci_state() {
  # 这个提交的 CI：success / pending / failure，查不到时 unknown。
  # 通过过一次就算 success。被后来的推送取消的那次（cancelled）不算没过，等最新的结果。
  local slug json
  slug="${DEPLOY_REPO:-$(git -C "$REPO" remote get-url origin \
    | sed -E -e 's#^.*github\.com[:/]##' -e 's#\.git$##' -e 's#/$##')}"
  if ! json="$(curl -fsS --max-time 20 -H 'Accept: application/vnd.github+json' \
    "https://api.github.com/repos/${slug}/commits/${1}/check-runs?check_name=${CHECK_NAME}" 2> /dev/null)"; then
    echo unknown
    return 0
  fi
  printf '%s' "$json" | python3 -c '
import json, sys

try:
    runs = json.load(sys.stdin).get("check_runs")
except (ValueError, AttributeError):
    runs = None
if not isinstance(runs, list):
    print("unknown")
elif any(run.get("conclusion") == "success" for run in runs):
    print("success")
elif not runs:
    print("pending")
else:
    run = max(runs, key=lambda item: item.get("id") or 0)
    if run.get("status") != "completed" or run.get("conclusion") in ("cancelled", "skipped", "stale"):
        print("pending")
    else:
        print("failure")
' 2> /dev/null || echo unknown
}

deploy_main() {
  local fetch_err target short last_sha="" last_state="" tries=1 current dirty ahead busy ci age before backup_dir=""

  take_lock
  # 网速掉到每秒 1KB 以下超过一分钟就放弃这一轮，不让一次卡住的拉取挡住以后的每一轮。
  if ! fetch_err="$(git -C "$REPO" -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=60 \
    fetch --quiet origin "$BRANCH" 2>&1)"; then
    unreachable "取不到 origin/${BRANCH}：$(printf '%s' "$fetch_err" | tail -n 1)"
  fi
  target="$(git -C "$REPO" rev-parse "origin/$BRANCH")"
  short="${target:0:7}"

  if [ -f "$LAST" ]; then
    read -r last_sha last_state < "$LAST" || true
  fi
  if [ "$last_sha" = "$target" ] && [ "${FORCE:-0}" != "1" ]; then
    if [ "$last_state" = "ok" ]; then
      touch "$SEEN"
      # 线上就是 main 上最新的提交，以前提示的「取不到 main」之类已经过去了。
      if grep -q '"ok":false' "$STATUS_DIR/${TASK}.json" 2> /dev/null; then
        status_ok "线上已经是 main 上最新的 ${short}"
      fi
      exit 0
    fi
    case "$last_state" in
      [0-9]*)
        if [ "$last_state" -ge "$MAX_TRIES" ]; then
          touch "$SEEN"
          exit 0
        fi
        tries=$((last_state + 1))
        ;;
    esac
  fi

  current="$(git -C "$REPO" symbolic-ref --quiet --short HEAD || true)"
  if [ "$current" != "$BRANCH" ]; then
    status_fail "Mac mini 上的仓库在 ${current:-游离的提交}上，不在 ${BRANCH}，没有自动上线"
    exit 0
  fi
  # 本机改过的文件会被一起打进镜像，上线的就不是 main 上的代码了。
  dirty="$(git -C "$REPO" -c core.quotePath=false status --porcelain --untracked-files=no \
    | awk 'NR <= 3 { print substr($0, 4) }' | tr '\n' ' ')"
  if [ -n "$dirty" ]; then
    status_fail "Mac mini 上的仓库有没提交的改动（${dirty% }），没有自动上线：还原它，要留的走 PR，见 docs/deploy.md「更新」"
    exit 0
  fi
  # 本机 main 上没推送的提交也一样：merge --ff-only 说已经是最新，建出来的镜像带着它们，CI 查的却是 origin/main。
  if ! git -C "$REPO" merge-base --is-ancestor HEAD "$target"; then
    ahead="$(git -C "$REPO" rev-list --count "$target..HEAD")"
    status_fail "Mac mini 上的 ${BRANCH} 有 ${ahead} 个没推送的提交，没有自动上线：存到分支走 PR，再回到 origin/${BRANCH}，见 docs/deploy.md「更新」"
    exit 0
  fi

  if [ "${FORCE:-0}" != "1" ]; then
    busy="$(busy_job)"
    if [ -n "$busy" ]; then
      note "${busy} 还在跑，跑完再上线 ${short}"
      exit 0
    fi
    ci="$(ci_state "$target")"
    case "$ci" in
      success) ;;
      pending)
        touch "$SEEN"
        age=$(($(date +%s) - $(git -C "$REPO" log -1 --format=%ct "$target")))
        if [ "$age" -gt 7200 ]; then
          status_fail "main 上的 ${short} 两小时还没有 CI 结果，没有上线（FORCE=1 scripts/deploy.sh 可以不等）"
        fi
        note "${short} 的 CI 还没跑完，下一轮再看"
        exit 0
        ;;
      failure)
        touch "$SEEN"
        status_fail "main 上的 ${short} CI 没通过，没有上线"
        exit 0
        ;;
      *)
        unreachable "查不到 ${short} 的 CI 结果（GitHub 接口出错）"
        ;;
    esac
  fi
  touch "$SEEN"

  before="$(git -C "$REPO" rev-parse HEAD)"
  # 迁移在应用启动时跑，跑了就退不回去：先备份。找不到备份目录就不上线，每轮再看。
  if ! git -C "$REPO" diff --quiet "$before" "$target" -- "$MIGRATIONS"; then
    backup_dir="${BACKUP_DIR:-$(saved_backup_dir)}"
    if [ -z "$backup_dir" ]; then
      status_fail "${short} 带新的数据库迁移，上线前要先备份，可是找不到备份目录，没有上线：带上 BACKUP_DIR 重跑 scripts/install-launchd.sh，见 docs/deploy.md「更新」"
      exit 0
    fi
  fi
  note "上线 ${short}（现在是 ${before:0:7}，第 ${tries} 次）"
  echo "$target $tries" > "$LAST"
  if [ -n "$backup_dir" ]; then
    note "${short} 带新的数据库迁移，先备份到 ${backup_dir}"
    run_step "迁移前备份" env BACKUP_DIR="$backup_dir" "$REPO/scripts/backup.sh"
  fi
  run_step "拉代码" git -C "$REPO" merge --ff-only --quiet "origin/$BRANCH"

  export GIT_SHA="$target"
  if ! attempt compose up -d --build; then
    go_back "$before" "重建并重启失败${LAST_ERR:+：}${LAST_ERR}"
    exit 1
  fi
  if ! wait_serving "$target"; then
    # 起不来多半是代码或迁移的问题，再试一次还是要停好几分钟，不自动重试。
    # 退回去会换掉这个容器，它的日志也跟着没了，先存下来。
    echo "$target $MAX_TRIES" > "$LAST"
    compose logs --no-color --tail 300 app > "$STATUS_DIR/deploy-failed.log" 2>&1 || true
    go_back "$before" "上线 ${short} 后 ${DEPLOY_WAIT} 秒 /health 都没报出 ${short}" \
      "起不来时的应用日志在 data/status/deploy-failed.log，修好再合；这个提交不再自动重试，FORCE=1 scripts/deploy.sh 可以再试"
    exit 1
  fi

  if ! git -C "$REPO" diff --quiet "$before" "$target" -- deploy/launchd scripts/install-launchd.sh; then
    run_step "重装定时任务" env DEPLOY_SELF=1 "$REPO/scripts/install-launchd.sh"
  fi

  echo "$target ok" > "$LAST"
  status_ok "上线 ${short}：$(git -C "$REPO" log -1 --format=%s "$target")"
  # 换下来的旧镜像没了名字还占着盘。删不掉不算上线失败。
  if ! attempt docker image prune -f --filter "label=${IMAGE_LABEL}"; then
    note "清理旧镜像没成功：${LAST_ERR}"
  fi
  note "上线完成 ${short}"
}

deploy_main "$@"; exit $?
