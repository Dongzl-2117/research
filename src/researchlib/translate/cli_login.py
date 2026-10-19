"""research 自有的 xAI device-code 登录。token 写 DATA_DIR/secrets/xai-oauth.json。"""

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
    elif status.needs_login:
        print(f"需要重新登录{('：' + status.last_error) if status.last_error else ''}。")
        print("运行：research-xai-login login")
    else:
        print("未登录。运行：research-xai-login login")
    if status.pending:
        print(f"进行中的验证码：{status.pending['userCode']}")
        print(status.pending.get("verificationUriComplete") or status.pending.get("verificationUri"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="research 的 xAI / Grok OAuth 登录（自有 refresh，勿与 AIHOT 共用）")
    parser.add_argument("command", nargs="?", default="login", choices=["login", "status", "logout"])
    args = parser.parse_args(argv)
    settings = Settings.from_env()
    try:
        if args.command == "status":
            _print_status(settings)
            return 0
        if args.command == "logout":
            status = logout_xai(settings.data_dir)
            print("已退出，本地 token 已删除。AIHOT / Hermes 的登录不受影响。")
            print(f"token 文件：{status.token_path}")
            return 0
        # login
        started = begin_device_login(settings.data_dir)
        pending = started.pending
        if not pending:
            print("没有拿到验证码", file=sys.stderr)
            return 1
        print("xAI 设备码登录。这个 refresh token 只属于 research，不要和 AIHOT / Hermes 共用。")
        print("")
        print("在浏览器打开：")
        print(f"  {pending.get('verificationUriComplete') or pending.get('verificationUri')}")
        print(f"验证码：{pending['userCode']}")
        print("")
        print("用有 Grok 订阅的账号授权。这一步在你自己的浏览器里完成。")
        done = wait_for_device_login(settings.data_dir)
        print("")
        print("已登录。access token 会在过期前自动刷新，旋转后的 refresh token 写回：")
        print(f"  {done.token_path}")
        print("文件权限 600，目录权限 700。不要提交进 git。")
        print(f"（默认路径也可写作 {xai_token_path(settings.data_dir)}）")
        return 0
    except XaiLoginRequiredError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
