"""xAI OAuth（device-code）。照 AIHOT packages/backend/src/providers/xai-oauth.ts 移植。

token 在 ``XAI_OAUTH_TOKEN_PATH``（compose 里是 /data/secrets/xai-oauth.json），用
``research-xai-login login`` 登录。几个进程靠同目录的 .lock 文件轮流刷新同一份 refresh token；
refresh token 每次刷新都会换，不要复制一份到别处用，也不接 Hermes 的 auth.json（格式会被拒绝）。
写入后 owner 按 XAI_OAUTH_FILE_UID/GID 设，没设时沿用原文件的。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

XAI_OAUTH_ISSUER = "https://auth.x.ai"
XAI_OAUTH_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
XAI_OAUTH_SCOPE = "openid profile email offline_access grok-cli:access api:access"
XAI_DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
XAI_INFERENCE_BASE_URL = "https://api.x.ai/v1"

AUTH_TIMEOUT_S = 15
LOCK_STALE_S = 20
LOCK_WAIT_S = 30
DEFAULT_SKEW_S = 60
TERMINAL_REFRESH = frozenset(
    {"invalid_grant", "invalid_token", "invalid_client", "unauthorized_client", "access_denied"}
)


class XaiLoginRequiredError(RuntimeError):
    pass


@dataclass
class XaiOAuthStatus:
    logged_in: bool
    needs_login: bool
    expires_at: str | None
    access_expires_in_seconds: int | None
    token_path: str
    last_error: str | None
    pending: dict[str, Any] | None


def xai_token_path(data_dir: Path | None = None) -> Path:
    override = os.environ.get("XAI_OAUTH_TOKEN_PATH", "").strip()
    if override:
        return Path(override)
    root = data_dir or Path(os.environ.get("DATA_DIR", "data"))
    if not root.is_absolute():
        root = Path.cwd() / root
    return root / "secrets" / "xai-oauth.json"


def _pending_path(token_path: Path) -> Path:
    return Path(str(token_path) + ".pending")


def _issuer() -> str:
    return (os.environ.get("XAI_OAUTH_ISSUER", "").strip() or XAI_OAUTH_ISSUER).rstrip("/")


def _client_id() -> str:
    return os.environ.get("XAI_OAUTH_CLIENT_ID", "").strip() or XAI_OAUTH_CLIENT_ID


def _scope() -> str:
    return os.environ.get("XAI_OAUTH_SCOPE", "").strip() or XAI_OAUTH_SCOPE


def _read_json(path: Path) -> Any | str:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unreadable"
    except json.JSONDecodeError:
        return "unreadable"


def _is_foreign(obj: Any) -> bool:
    if not isinstance(obj, dict):
        return False
    if obj.get("version") == 1 and isinstance(obj.get("access_token"), str):
        return False
    return "providers" in obj or "tokens" in obj or isinstance(obj.get("refresh_token"), str) or isinstance(
        obj.get("access_token"), str
    )


def _parse_stored(obj: Any) -> dict[str, Any] | str | None:
    if obj == "missing":
        return None
    if obj == "unreadable" or _is_foreign(obj):
        return "corrupt"
    if not isinstance(obj, dict) or obj.get("version") != 1:
        return "corrupt"
    return {
        "version": 1,
        "access_token": obj.get("access_token") if isinstance(obj.get("access_token"), str) else "",
        "refresh_token": obj.get("refresh_token") if isinstance(obj.get("refresh_token"), str) else "",
        "token_type": "Bearer",
        "expires_at": float(obj["expires_at"]) if isinstance(obj.get("expires_at"), (int, float)) else 0.0,
        "obtained_at": obj.get("obtained_at") if isinstance(obj.get("obtained_at"), str) else "1970-01-01T00:00:00Z",
        "needs_login": obj.get("needs_login") is True,
        "last_error": obj.get("last_error") if isinstance(obj.get("last_error"), str) else None,
    }


def _desired_owner(path: Path) -> tuple[int, int] | None:
    """Owner for the token/lock files.

    Prefer XAI_OAUTH_FILE_UID/GID; else keep the existing file or secrets dir owner.
    Skip when that would be root-only and no env is set.
    """
    uid_s = os.environ.get("XAI_OAUTH_FILE_UID", "").strip()
    gid_s = os.environ.get("XAI_OAUTH_FILE_GID", "").strip()
    if uid_s.isdigit() and gid_s.isdigit():
        return int(uid_s), int(gid_s)
    for candidate in (path, path.parent):
        try:
            st = candidate.stat()
        except OSError:
            continue
        if st.st_uid == 0 and st.st_gid == 0:
            continue
        return st.st_uid, st.st_gid
    return None


def _chown_secret(path: Path) -> None:
    owner = _desired_owner(path)
    if owner is None:
        return
    try:
        os.chown(path, owner[0], owner[1])
    except OSError:
        pass


def _write_secret(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    # Remember owner before replace (tmp is often created as root in the app container).
    prior = _desired_owner(path)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{os.urandom(4).hex()}")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    if prior is not None:
        try:
            os.chown(tmp, prior[0], prior[1])
        except OSError:
            pass
    tmp.replace(path)
    os.chmod(path, 0o600)
    if prior is not None:
        try:
            os.chown(path, prior[0], prior[1])
        except OSError:
            pass
    else:
        _chown_secret(path)


def _assert_own(token_path: Path) -> None:
    obj = _read_json(token_path)
    if obj not in ("missing", "unreadable") and _is_foreign(obj):
        raise XaiLoginRequiredError(
            "拒绝写入：这个路径不是 research 的 xAI token 文件（疑似 Hermes 或其它格式）。"
            "请检查 XAI_OAUTH_TOKEN_PATH（compose 里是 /data/secrets/xai-oauth.json）。"
        )


class _FileLock:
    def __init__(self, token_path: Path) -> None:
        self.lock_path = Path(str(token_path) + ".lock")
        self._token_path = token_path

    def __enter__(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        started = time.time()
        while True:
            try:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                try:
                    os.write(fd, str(os.getpid()).encode())
                finally:
                    os.close(fd)
                # Same owner as the token file, so another process can remove a stale lock.
                owner = _desired_owner(self._token_path)
                if owner is not None:
                    try:
                        os.chown(self.lock_path, owner[0], owner[1])
                    except OSError:
                        pass
                return
            except FileExistsError:
                try:
                    age = time.time() - self.lock_path.stat().st_mtime
                    if age > LOCK_STALE_S:
                        self.lock_path.unlink(missing_ok=True)
                        continue
                except OSError:
                    continue
                if time.time() - started > LOCK_WAIT_S:
                    raise RuntimeError("xAI OAuth 文件锁超时") from None
                time.sleep(0.015)

    def __exit__(self, *exc: object) -> None:
        self.lock_path.unlink(missing_ok=True)


def _post_form(url: str, fields: dict[str, str]) -> tuple[int, Any]:
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"accept": "application/json", "content-type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=AUTH_TIMEOUT_S) as resp:
            text = resp.read().decode("utf-8", errors="replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        status = exc.code
    except urllib.error.URLError as exc:
        raise RuntimeError(f"xAI OAuth 请求失败：{exc}") from exc
    try:
        payload = json.loads(text) if text else None
    except json.JSONDecodeError:
        payload = None
    return status, payload


def _string_field(obj: Any, key: str) -> str | None:
    if not isinstance(obj, dict):
        return None
    value = obj.get(key)
    return value if isinstance(value, str) and value.strip() else None


def _positive_number(obj: Any, key: str) -> float | None:
    if not isinstance(obj, dict):
        return None
    value = obj.get(key)
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    if isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            return None
        return number if number > 0 else None
    return None


def _endpoints() -> tuple[str, str]:
    issuer = _issuer()
    fallback_device = f"{issuer}/oauth2/device/code"
    fallback_token = f"{issuer}/oauth2/token"
    device_override = os.environ.get("XAI_OAUTH_DEVICE_URL", "").strip()
    token_override = os.environ.get("XAI_OAUTH_TOKEN_URL", "").strip()
    if device_override or token_override:
        return device_override or fallback_device, token_override or fallback_token
    try:
        req = urllib.request.Request(
            f"{issuer}/.well-known/openid-configuration",
            headers={"accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=AUTH_TIMEOUT_S) as resp:
            conf = json.loads(resp.read().decode())
        device = conf.get("device_authorization_endpoint") or fallback_device
        token = conf.get("token_endpoint") or fallback_token
        return str(device), str(token)
    except Exception:
        return fallback_device, fallback_token


def _expiry_ms(access: str, expires_in: float | None, now_ms: float) -> float:
    if expires_in is not None:
        return now_ms + expires_in * 1000
    try:
        import base64

        part = access.split(".")[1]
        pad = "=" * (-len(part) % 4)
        payload = json.loads(base64.urlsafe_b64decode(part + pad))
        exp = payload.get("exp")
        if isinstance(exp, (int, float)) and exp * 1000 > now_ms:
            return float(exp) * 1000
    except Exception:
        pass
    return now_ms + 900_000


def _tokens_from(obj: Any, previous_refresh: str, now_ms: float) -> dict[str, Any] | None:
    access = _string_field(obj, "access_token")
    if not access:
        return None
    rotated = _string_field(obj, "refresh_token")
    return {
        "version": 1,
        "access_token": access,
        "refresh_token": rotated or previous_refresh,
        "token_type": "Bearer",
        "expires_at": _expiry_ms(access, _positive_number(obj, "expires_in"), now_ms),
        "obtained_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_ms / 1000)),
        "needs_login": False,
        "last_error": None,
    }


def _public_error(status: int, obj: Any) -> str:
    code = _string_field(obj, "error") or ""
    desc = _string_field(obj, "error_description") or ""
    return f"HTTP {status}{(' ' + code) if code else ''}{(': ' + desc) if desc else ''}"[:300]


def _present(tokens: dict[str, Any] | str | None, pending: dict[str, Any] | None, token_path: Path) -> XaiOAuthStatus:
    now = time.time() * 1000
    broken = tokens == "corrupt"
    stored = None if broken or not isinstance(tokens, dict) else tokens
    logged_in = bool(stored and not stored.get("needs_login") and stored.get("refresh_token"))
    expires_at = None
    left = None
    if logged_in and stored and stored["expires_at"] > 0:
        expires_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stored["expires_at"] / 1000))
        left = max(0, int((stored["expires_at"] - now) / 1000))
    pending_view = None
    if pending and isinstance(pending.get("user_code"), str):
        pending_view = {
            "userCode": pending["user_code"],
            "verificationUri": pending.get("verification_uri"),
            "verificationUriComplete": pending.get("verification_uri_complete"),
            "expiresAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(pending.get("expires_at", 0) / 1000)),
            "waitMs": max(0, int(pending.get("next_poll_at", 0) - now)),
            "lastError": pending.get("last_error"),
        }
    return XaiOAuthStatus(
        logged_in=logged_in,
        needs_login=broken or bool(stored and stored.get("needs_login")),
        expires_at=expires_at,
        access_expires_in_seconds=left,
        token_path=str(token_path),
        last_error=("token 文件无法使用，需要重新登录" if broken else (stored or {}).get("last_error") or (pending or {}).get("last_error")),
        pending=pending_view,
    )


def xai_oauth_status(data_dir: Path | None = None) -> XaiOAuthStatus:
    token_path = xai_token_path(data_dir)
    pending_raw = _read_json(_pending_path(token_path))
    pending = pending_raw if isinstance(pending_raw, dict) else None
    return _present(_parse_stored(_read_json(token_path)), pending, token_path)


def begin_device_login(data_dir: Path | None = None) -> XaiOAuthStatus:
    token_path = xai_token_path(data_dir)
    with _FileLock(token_path):
        device_url, _ = _endpoints()
        status, payload = _post_form(device_url, {"client_id": _client_id(), "scope": _scope()})
        if status != 200:
            raise RuntimeError(f"xAI 设备码申请失败：{_public_error(status, payload)}")
        device_code = _string_field(payload, "device_code")
        user_code = _string_field(payload, "user_code")
        verification = _string_field(payload, "verification_uri")
        if not device_code or not user_code or not verification:
            raise RuntimeError("xAI 设备码响应缺少 device_code、user_code 或 verification_uri")
        now = time.time() * 1000
        expires_in = _positive_number(payload, "expires_in") or 600
        interval = min(60, max(1, round(_positive_number(payload, "interval") or 5)))
        pending = {
            "device_code": device_code,
            "user_code": user_code,
            "verification_uri": verification,
            "verification_uri_complete": _string_field(payload, "verification_uri_complete"),
            "expires_at": now + expires_in * 1000,
            "interval_seconds": interval,
            "next_poll_at": now + interval * 1000,
            "last_error": None,
        }
        _write_secret(_pending_path(token_path), pending)
        return _present(_parse_stored(_read_json(token_path)), pending, token_path)


def poll_device_login(data_dir: Path | None = None) -> dict[str, Any]:
    token_path = xai_token_path(data_dir)
    with _FileLock(token_path):
        pending_raw = _read_json(_pending_path(token_path))
        if not isinstance(pending_raw, dict) or not pending_raw.get("user_code"):
            return {"status": "error", "error": "没有进行中的 xAI 登录", "terminal": True, "waitMs": 0}
        pending = pending_raw
        now = time.time() * 1000
        if pending.get("expires_at", 0) <= now:
            err = "验证码已过期，请重新登录"
            pending = {**pending, "device_code": "", "last_error": err}
            _write_secret(_pending_path(token_path), pending)
            return {"status": "error", "error": err, "terminal": True, "waitMs": 0}
        if not pending.get("device_code"):
            return {"status": "error", "error": pending.get("last_error") or "登录未完成", "terminal": True, "waitMs": 0}
        if now < pending.get("next_poll_at", 0):
            return {"status": "pending", "waitMs": pending["next_poll_at"] - now}
        _, token_url = _endpoints()
        try:
            status, payload = _post_form(
                token_url,
                {
                    "grant_type": XAI_DEVICE_GRANT,
                    "device_code": pending["device_code"],
                    "client_id": _client_id(),
                },
            )
        except RuntimeError as exc:
            return {"status": "error", "error": str(exc), "terminal": False, "waitMs": pending.get("interval_seconds", 5) * 1000}
        code = _string_field(payload, "error") or ""
        if status == 200:
            stored = _tokens_from(payload, "", now)
            if not stored or not stored.get("refresh_token"):
                return {"status": "error", "error": "xAI 登录响应没有 refresh_token", "terminal": True, "waitMs": 0}
            _assert_own(token_path)
            _write_secret(token_path, stored)
            _pending_path(token_path).unlink(missing_ok=True)
            return {"status": "success"}
        if code == "authorization_pending":
            wait = pending.get("interval_seconds", 5) * 1000
            pending = {**pending, "next_poll_at": now + wait}
            _write_secret(_pending_path(token_path), pending)
            return {"status": "pending", "waitMs": wait}
        if code == "slow_down":
            interval = min(60, pending.get("interval_seconds", 5) + 5)
            wait = interval * 1000
            pending = {**pending, "interval_seconds": interval, "next_poll_at": now + wait}
            _write_secret(_pending_path(token_path), pending)
            return {"status": "pending", "waitMs": wait}
        if status >= 500 or status == 429 or code == "temporarily_unavailable":
            return {"status": "error", "error": _public_error(status, payload), "terminal": False, "waitMs": pending.get("interval_seconds", 5) * 1000}
        err = _public_error(status, payload)
        pending = {**pending, "device_code": "", "last_error": err}
        _write_secret(_pending_path(token_path), pending)
        return {"status": "error", "error": err, "terminal": True, "waitMs": 0}


def wait_for_device_login(data_dir: Path | None = None) -> XaiOAuthStatus:
    transient = 0
    while True:
        result = poll_device_login(data_dir)
        if result["status"] == "success":
            return xai_oauth_status(data_dir)
        if result["status"] == "pending":
            transient = 0
            time.sleep(max(0.2, result.get("waitMs", 1000) / 1000))
            continue
        if not result.get("terminal") and transient < 5:
            transient += 1
            time.sleep(max(1.0, result.get("waitMs", 5000) / 1000))
            continue
        raise XaiLoginRequiredError(result.get("error") or "登录失败")


def _mark_needs_login(token_path: Path, message: str) -> None:
    _assert_own(token_path)
    _write_secret(
        token_path,
        {
            "version": 1,
            "access_token": "",
            "refresh_token": "",
            "token_type": "Bearer",
            "expires_at": 0,
            "obtained_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "needs_login": True,
            "last_error": message[:300],
        },
    )


def _skew_for(state: dict[str, Any]) -> float:
    raw = os.environ.get("XAI_OAUTH_REFRESH_SKEW_SECONDS", "").strip()
    configured = int(raw) * 1000 if raw.isdigit() else DEFAULT_SKEW_S * 1000
    try:
        obtained = time.mktime(time.strptime(state["obtained_at"][:19], "%Y-%m-%dT%H:%M:%S")) * 1000
    except Exception:
        obtained = state["expires_at"] - 900_000
    life = state["expires_at"] - obtained
    if life <= 0:
        return 0
    return min(configured, max(5_000, life / 2))


def _refresh(state: dict[str, Any]) -> dict[str, Any]:
    _, token_url = _endpoints()
    status, payload = _post_form(
        token_url,
        {
            "grant_type": "refresh_token",
            "refresh_token": state["refresh_token"],
            "client_id": _client_id(),
        },
    )
    if status != 200:
        message = _public_error(status, payload)
        code = _string_field(payload, "error") or ""
        desc = _string_field(payload, "error_description") or ""
        if code in TERMINAL_REFRESH or status == 401 or re_revoked(desc):
            raise XaiLoginRequiredError(f"xAI OAuth 需要重新登录：{message}")
        raise RuntimeError(f"xAI OAuth 刷新失败：{message}")
    next_tokens = _tokens_from(payload, state["refresh_token"], time.time() * 1000)
    if not next_tokens:
        raise RuntimeError("xAI OAuth 刷新响应缺少 access_token")
    return next_tokens


def re_revoked(desc: str) -> bool:
    import re

    return bool(re.search(r"revoked|reused|already been used", desc, re.I))


def get_xai_access_token(
    data_dir: Path | None = None,
    *,
    force: bool = False,
    discard_access_token: str | None = None,
) -> str:
    token_path = xai_token_path(data_dir)
    with _FileLock(token_path):
        _assert_own(token_path)
        state = _parse_stored(_read_json(token_path))
        if not state or state == "corrupt" or state.get("needs_login") or not state.get("refresh_token"):
            if state == "corrupt":
                raise XaiLoginRequiredError("xAI OAuth 的 token 文件无法使用。请重新登录：docker compose exec app research-xai-login login")
            if isinstance(state, dict) and state.get("needs_login"):
                raise XaiLoginRequiredError(
                    f"xAI OAuth 需要重新登录{('：' + state['last_error']) if state.get('last_error') else ''}。请重新登录：docker compose exec app research-xai-login login"
                )
            raise XaiLoginRequiredError("xAI OAuth 尚未登录。请先登录：docker compose exec app research-xai-login login")
        now = time.time() * 1000
        skew = _skew_for(state)
        fresh = bool(state["access_token"]) and state["expires_at"] - now > skew
        replaced = bool(discard_access_token) and state["access_token"] != discard_access_token
        if fresh and (not force or replaced):
            return state["access_token"]
        try:
            nxt = _refresh(state)
            _write_secret(token_path, nxt)
            return nxt["access_token"]
        except XaiLoginRequiredError as exc:
            _mark_needs_login(token_path, str(exc))
            raise
        except Exception:
            if state["access_token"] and state["expires_at"] > now:
                return state["access_token"]
            raise


def is_stale_xai_access(status: int, body: str) -> bool:
    if status == 401:
        return True
    if status != 403:
        return False
    import re

    return bool(re.search(r"unauthenticated:|bad-credentials|OAuth2 access token could not be validated", body, re.I))


def logout_xai(data_dir: Path | None = None) -> XaiOAuthStatus:
    token_path = xai_token_path(data_dir)
    with _FileLock(token_path):
        obj = _read_json(token_path)
        if obj not in ("missing", "unreadable") and _is_foreign(obj):
            raise XaiLoginRequiredError("拒绝删除：这个路径不是 research 的 xAI token 文件（疑似 Hermes 格式）。")
        directory = token_path.parent
        base = token_path.name
        if directory.is_dir():
            for child in directory.iterdir():
                if child.name == base or child.name.startswith(base + "."):
                    child.unlink(missing_ok=True)
        return _present(None, None, token_path)
