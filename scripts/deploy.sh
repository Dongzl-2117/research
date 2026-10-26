#!/bin/bash
# 自动上线：main 上有新提交，而且这个提交在 GitHub 上的 CI（Tests）通过了，就拉代码、重建并重启应用。
# 定时任务 com.zldong.research.deploy 每 10 分钟跑一次（scripts/install-launchd.sh 装），合并进 main 的改动
# 等 CI 跑完再等下一轮，一般 10 到 20 分钟上线。迁移和后台重算在应用启动时自己做。
#
#   scripts/deploy.sh              # 手动跑一次：有新提交并且 CI 过了就上线
#   FORCE=1 scripts/deploy.sh      # 不等 CI、不等别的定时任务，上次失败过的提交也重试
#
# 结果写进 data/status/deploy.json，首页只在失败时提示：拉代码、重建失败，重启后应用没起来，CI 没过，
# 仓库不在 main 或者有没提交的改动，或者 6 小时取不到 main、查不到 CI。偶尔断网的那一轮什么都不记，下一轮再试。
# 重建失败的提交最多试 3 轮，之后等下一个提交或 FORCE=1。CI 没过的提交每轮再查，CI 重跑通过了就上线。
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
# 重建时会被打断的定时任务（scripts/ 下的脚本名）。
JOBS="nightly crawl-fangwei-mp crawl-fangwei-zsxq ingest-aihot backup restore-check"
# launchd 里没有终端：git 要密码时直接失败，不要卡住等输入。
export GIT_TERMINAL_PROMPT=0
. "$REPO/scripts/status.sh"
LAST="$STATUS_DIR/.deploy-last"
SEEN="$STATUS_DIR/.deploy-seen"
LOCK="$STATUS_DIR/.deploy.lock"
locked=0
trap 'STATUS_FAILED_LINE=$LINENO; STATUS_FAILED_CMD=$BASH_COMMAND' ERR
trap 'rc=$?; if [ "$locked" = "1" ]; then rm -rf "$LOCK"; fi; status_finish "$rc"' EXIT

compose() {
  docker compose --project-directory "$REPO" -f "$REPO/docker-compose.yml" "$@"
}

note() {
  echo "$(date '+%F %T') $*"
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
  local fetch_err target short last_sha="" last_state="" tries=1 current dirty busy ci age before waited

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
    status_fail "Mac mini 上的仓库有没提交的改动（${dirty% }），没有自动上线：先提交或还原"
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
  note "上线 ${short}（现在是 ${before:0:7}，第 ${tries} 次）"
  echo "$target $tries" > "$LAST"
  run_step "拉代码" git -C "$REPO" merge --ff-only --quiet "origin/$BRANCH"
  run_step "重建并重启" compose up -d --build

  waited=0
  until curl -fsS -o /dev/null --max-time 5 "$APP_URL" 2> /dev/null; do
    if [ "$waited" -ge "$DEPLOY_WAIT" ]; then
      status_fail "上线 ${short} 后应用 ${DEPLOY_WAIT} 秒没起来：看 docker compose logs app"
      exit 1
    fi
    sleep 5
    waited=$((waited + 5))
  done

  if ! git -C "$REPO" diff --quiet "$before" "$target" -- deploy/launchd scripts/install-launchd.sh; then
    run_step "重装定时任务" env DEPLOY_SELF=1 "$REPO/scripts/install-launchd.sh"
  fi

  echo "$target ok" > "$LAST"
  status_ok "上线 ${short}：$(git -C "$REPO" log -1 --format=%s "$target")"
  note "上线完成 ${short}"
}

deploy_main "$@"; exit $?
