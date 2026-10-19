"""xAI device-code 登录。默认与 AIHOT 共用 aihot_data 卷上的 xai-oauth.json。"""

from __future__ import annotations

import argparse
import sys

from researchlib.config import Settings
from researchlib.translate.xai_oauth import (
    XaiLoginRequiredError,
    begin_device_login,
    logout_xai,
    wait_for_device_login,
    xai_oauth_status,
    xai_token_path,
)

AIHOT_LOGIN_HINT = "cd ~/aihot && docker compose exec api node scripts/xai-login.ts login"


def _print_status(settings: Settings) -> None:
    status = xai_oauth_status(settings.data_dir)
    print(f"token 文件：{status.token_path}")
    if status.logged_in:
        left = status.access_expires_in_seconds or 0
        if left > 0:
            print(f"已登录。access token 约 {max(1, (left + 59) // 60)} 分钟后过期，到期前会自动刷新。")
        else:
            print("已登录。access token 已过期，下次调用会刷新。")
        if status.expires_at:
            print(f"access 到期：{status.expires_at}")
        print("与 AIHOT 共用同一份 OAuth 凭据；一般无需再单独授权。")
    elif status.needs_login:
        print(f"需要重新登录{('：' + status.last_error) if status.last_error else ''}。")
        print(f"请在 AIHOT 侧登录：{AIHOT_LOGIN_HINT}")
    else:
        print("未登录。")
        print(f"请在 AIHOT 侧登录：{AIHOT_LOGIN_HINT}")
    if status.pending:
        print(f"进行中的验证码：{status.pending['userCode']}")
        print(status.pending.get("verificationUriComplete") or status.pending.get("verificationUri"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="xAI / Grok OAuth（默认与 AIHOT 共用 aihot_data 上的 token）"
    )
    parser.add_argument("command", nargs="?", default="status", choices=["login", "status", "logout"])
    args = parser.parse_args(argv)
    settings = Settings.from_env()
    try:
        if args.command == "status":
            _print_status(settings)
            return 0
        if args.command == "logout":
            status = logout_xai(settings.data_dir)
            print("已退出，token 文件已删除。")
            print("注意：与 AIHOT 共用同一文件时，AIHOT 也会一并退出，需重新登录。")
            print(f"token 文件：{status.token_path}")
            return 0
        # login（备用；优先走 AIHOT 侧登录）
        print("提示：生产环境请优先在 AIHOT 侧登录，research 会自动读同一份凭据。")
        print(f"  {AIHOT_LOGIN_HINT}")
        print("继续用 research 发起 device-code（写入同一 token 路径）…")
        print("")
        started = begin_device_login(settings.data_dir)
        pending = started.pending
        if not pending:
            print("没有拿到验证码", file=sys.stderr)
            return 1
        print("在浏览器打开：")
        print(f"  {pending.get('verificationUriComplete') or pending.get('verificationUri')}")
        print(f"验证码：{pending['userCode']}")
        print("")
        print("用有 Grok 订阅的账号授权。这一步在你自己的浏览器里完成。")
        done = wait_for_device_login(settings.data_dir)
        print("")
        print("已登录。access token 会在过期前自动刷新，旋转后的 refresh token 写回：")
        print(f"  {done.token_path}")
        print("文件权限 600，目录权限 700；owner 会设回 AIHOT uid（默认 1000）。不要提交进 git。")
        print(f"（路径解析：{xai_token_path(settings.data_dir)}）")
        return 0
    except XaiLoginRequiredError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
