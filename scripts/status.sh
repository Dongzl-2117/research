# 定时脚本共用（用 . 引入，不单独跑）：把这次的结果写进 data/status/<任务>.json。
# 出问题（失败了，或者太久没成功）时值班助手跑 docker compose exec app research-status 看得到；
# 首页不显示（见 src/researchlib/health.py）。
#
#   TASK=backup
#   . "$REPO/scripts/status.sh"
#   trap 'STATUS_FAILED_LINE=$LINENO; STATUS_FAILED_CMD=$BASH_COMMAND' ERR
#   trap 'rc=$?; status_finish "$rc"' EXIT
#   run_step "导出数据库" compose exec -T db pg_dump … > "$partial"
#   status_ok "备份完成：…"
#
# run_step 失败时记下「哪一步失败、错误输出的最后一行」，再按原退出码退出。没走 run_step 的意外失败，
# 由 EXIT 时的 status_finish 补一条「第几行、哪条命令」。
#
# 只用 macOS 自带 bash 3.2 也有的写法。变量后面紧跟中文时一律写成 ${变量}，
# 否则 bash 3.2 会把紧跟的全角字符算进变量名。

STATUS_DIR="${STATUS_DIR:-$REPO/data/status}"
STATUS_FAILED_LINE=""
STATUS_FAILED_CMD=""

status_json_escape() {
  local s="$1"
  s="${s//\\/\\\\}"
  s="${s//\"/\\\"}"
  s="${s//$'\n'/ }"
  s="${s//$'\r'/ }"
  s="${s//$'\t'/ }"
  # 其余控制字符（终端转义码这类）删掉：写进去 JSON 就坏了，health.read_status 读不出来，这条失败就不报了。
  printf '%s' "$s" | LC_ALL=C tr -d '\000-\037\177'
}

status_write() {
  # status_write true|false 消息。写完留一个标记，子 shell 里写的父进程也认得（$$ 在子 shell 里不变）。
  local now tmp warning_json=""
  now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  mkdir -p "$STATUS_DIR" 2>/dev/null || return 0
  tmp="$STATUS_DIR/.${TASK}.json.$$"
  if [ -n "${3:-}" ]; then
    warning_json="$(printf ',"warning":"%s"' "$(status_json_escape "$3")")"
  fi
  if printf '{"task":"%s","ok":%s,"at":"%s","message":"%s"%s}\n' \
    "$TASK" "$1" "$now" "$(status_json_escape "$2")" "$warning_json" > "$tmp"; then
    mv -f "$tmp" "$STATUS_DIR/${TASK}.json"
  fi
  : > "$STATUS_DIR/.${TASK}.$$.done" 2>/dev/null || true
}

status_ok() {
  status_write true "$1" "${2:-}"
}

status_fail() {
  status_write false "$1"
}

compose_exec() {
  # docker compose exec 在容器没起来时会退出，错误是 service "<名字>" is not running。
  # 夜间识图踩过。先确认服务在跑，没有就 up -d，再等几秒直到 exec 能进去。
  # 调用方要先定义 compose（docker compose --project-directory …）。
  local svc="$1"
  shift
  if ! compose ps --status running --services 2>/dev/null | grep -qx "${svc}"; then
    echo "$(date '+%F %T') ${svc} 没在跑，先启动" >&2
    compose up -d "${svc}"
  fi
  local attempt=0
  while [ "$attempt" -lt 8 ]; do
    if compose exec -T "${svc}" true >/dev/null 2>&1; then
      compose exec -T "${svc}" "$@"
      return $?
    fi
    attempt=$((attempt + 1))
    sleep 2
  done
  echo "service ${svc} is not running" >&2
  return 1
}

run_step() {
  # run_step 名字 命令…：命令的标准输出照常（可以重定向到文件），错误输出先收着、跑完再打出来。
  local name="$1"
  shift
  local err rc last
  err="$(mktemp "${TMPDIR:-/tmp}/research-step.XXXXXX")"
  if "$@" 2> "$err"; then
    cat "$err" >&2
    rm -f "$err"
    return 0
  else
    rc=$?
  fi
  cat "$err" >&2
  last="$(grep -v '^[[:space:]]*$' "$err" | tail -n 1 || true)"
  rm -f "$err"
  status_fail "${name}失败（退出码 ${rc}）${last:+：}${last}"
  exit "$rc"
}

status_finish() {
  # 脚本退出时：这次已经写过结果的不再写；没写过又是非零退出，补一条失败记录。
  local rc="$1" marker="$STATUS_DIR/.${TASK}.$$.done"
  if [ -f "$marker" ]; then
    rm -f "$marker"
    return 0
  fi
  if [ "$rc" -ne 0 ]; then
    status_write false "退出码 ${rc}，第 ${STATUS_FAILED_LINE:-?} 行：${STATUS_FAILED_CMD:-}"
    rm -f "$marker"
  fi
}

# 新抓进来的文章当场处理完（识图、译制、判断、摘要、段落打分），不等夜间、不避开 DeepSeek 高峰价
# （Zhilin 11-19：新抓的直接来，只有大面积的才等）。抓取前 new_mark 记下库里最大的文章 id，导入以后
#   run_step "处理新文章" process_new 来源 "$mark"
# 只处理这一来源 id 比它大的几篇，见 src/researchlib/fresh.py。调用方要先定义 compose。
new_mark() {
  compose_exec app research-process-new --last-id
}

new_ocr_enabled() {
  # 识图跟着夜间任务的开关：环境变量 NIGHTLY_OCR，没设就读 install-launchd.sh 记下的 data/launchd.env，
  # 再没有就看装好的夜间任务。夜间不识图的话抓取时也不识。
  local value="${NIGHTLY_OCR:-}"
  if [ -z "$value" ] && [ -f "$REPO/data/launchd.env" ]; then
    value="$(sed -n "s/^SAVED_NIGHTLY_OCR=//p" "$REPO/data/launchd.env" | tail -n 1)"
  fi
  local plist="$HOME/Library/LaunchAgents/com.zldong.research.nightly.plist"
  if [ -z "$value" ] && [ -f "$plist" ] && [ -x /usr/libexec/PlistBuddy ]; then
    value="$(/usr/libexec/PlistBuddy -c "Print :EnvironmentVariables:NIGHTLY_OCR" "$plist" 2>/dev/null || true)"
  fi
  [ "$value" = "1" ]
}

process_new() {
  # process_new 来源 起点id：PROCESS_NEW=0 时跳过（只抓取导入，留给夜间）。
  local source="$1" mark="$2"
  if [ "${PROCESS_NEW:-1}" = "0" ]; then
    echo "$(date '+%F %T') 跳过处理新文章（PROCESS_NEW=0），留给夜间"
    return 0
  fi
  if new_ocr_enabled; then
    compose_exec app research-process-new --source "$source" --after-id "$mark" --ocr
  else
    compose_exec app research-process-new --source "$source" --after-id "$mark"
  fi
}
