#!/bin/bash
# 依赖锁文件，版本和 hash 都锁死：
#   requirements.lock      应用镜像。Dockerfile 用 pip install --require-hashes 只装这里面的
#   requirements-dev.lock  CI 和本地测试：应用的依赖加 pytest、ruff、httpx，版本和上面一致
#   requirements-ocr.lock  ocr 镜像：再加 RapidOCR，版本和上面一致
#
#   scripts/lock.sh                            # pyproject.toml 改了依赖以后：已锁的版本不动，只补新加的
#   scripts/lock.sh --upgrade                  # 全部升到最新
#   scripts/lock.sh --upgrade-package fastapi  # 只升一个（可以写多次）
#   scripts/lock.sh --check                    # CI 用：锁文件和 pyproject.toml 对不上就失败，并打印该有的内容
#
# 按 Linux、Python 3.12 解析（和镜像一致）；hash 包含各平台的安装包，Mac mini（arm64）上构建一样能装。
# 用 uv 解析。本机没有 uv 时在 python:3.12 容器里装一个再跑（要 Docker）。
#
# 只用 macOS 自带 bash 3.2 也有的写法。变量后面紧跟中文时一律写成 ${变量}。
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
UV_VERSION="${UV_VERSION:-0.12.20}"
PYTHON_IMAGE="python:3.12-slim-bookworm"
LOCKS="requirements.lock requirements-dev.lock requirements-ocr.lock"

if ! command -v uv > /dev/null 2>&1; then
  exec docker run --rm -v "${REPO}:/repo" -w /repo -e UV_VERSION="${UV_VERSION}" "${PYTHON_IMAGE}" \
    bash -c 'pip install --quiet --root-user-action=ignore "uv==${UV_VERSION}" && scripts/lock.sh "$@"' \
    lock ${1+"$@"}
fi

mode=write
upgrade=""
while [ $# -gt 0 ]; do
  case "$1" in
    --check) mode=check ;;
    --upgrade) upgrade="${upgrade} --upgrade" ;;
    --upgrade-package)
      if [ $# -lt 2 ]; then
        echo "--upgrade-package 后面要跟包名" >&2
        exit 2
      fi
      upgrade="${upgrade} --upgrade-package $2"
      shift
      ;;
    *)
      echo "不认识的参数：${1}" >&2
      exit 2
      ;;
  esac
  shift
done

cd "$REPO"
work="$(mktemp -d "${TMPDIR:-/tmp}/research-lock.XXXXXX")"
trap 'rm -rf "$work"' EXIT

if [ "$mode" = check ]; then
  # 在临时目录里重新解析。先把现有的锁文件拷过去：uv 会沿用里面已锁的版本，只有对不上时才改。
  target="$work/out"
  mkdir -p "$target"
  for name in $LOCKS; do
    if [ -f "$name" ]; then
      cp "$name" "$target/$name"
    fi
  done
else
  target="$REPO"
fi

compile() {
  # compile 输出文件 额外参数…
  local out="$1"
  shift
  # shellcheck disable=SC2086  # upgrade 里是几个参数，要按空格拆开
  uv pip compile pyproject.toml --quiet --python-version 3.12 --python-platform linux \
    --generate-hashes --no-header --output-file "$out" $upgrade "$@"
}

pins() {
  # 只取「包名==版本」，不比 hash：同一个版本后来补传了别的平台的安装包，hash 会变多。
  grep -oE '^[A-Za-z0-9][A-Za-z0-9._-]*==[^ ;\\]+' "$1" | sort
}

compile "$target/requirements.lock"
pins "$target/requirements.lock" > "$work/runtime.txt"
compile "$target/requirements-dev.lock" --extra dev --constraint "$work/runtime.txt"
compile "$target/requirements-ocr.lock" --extra ocr --constraint "$work/runtime.txt"

if [ "$mode" = write ]; then
  echo "已更新：${LOCKS}"
  exit 0
fi

status=0
report="$work/report.txt"
: > "$report"
for name in $LOCKS; do
  if [ ! -f "$REPO/$name" ]; then
    echo "缺少 ${name}" >> "$report"
    status=1
  elif ! diff -u "--label=${name}（仓库里的）" "--label=${name}（按 pyproject.toml 该有的）" \
    <(pins "$REPO/$name") <(pins "$target/$name") >> "$report"; then
    status=1
  fi
done
if [ "$status" -ne 0 ]; then
  # 该有的内容先打出来（日志里折叠着），出了什么问题放在最后，一眼能看到。
  for name in $LOCKS; do
    echo "::group::${name}（按 pyproject.toml 该有的）"
    cat "$target/$name"
    echo "::endgroup::"
  done
  cat "$report"
  echo "锁文件和 pyproject.toml 对不上：本机跑 scripts/lock.sh 重新生成，或者照上面的内容替换。"
fi
exit "$status"
