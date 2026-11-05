"""实验管理入口的认证契约；不提供默认登录，也不信任客户端/代理自报身份。"""
from __future__ import annotations

import hashlib
import hmac
import inspect
from dataclasses import dataclass
from urllib.parse import urlsplit

from fastapi import Request

ADMIN_MOUNT = "/admin/algorithms"
ADMIN_PAGE = ADMIN_MOUNT + "/experiments"


@dataclass(frozen=True)
class ExperimentOwner:
    """只能由服务端已验证的所有者认证适配器创建；session_id 必须绑定可撤销的会话。"""

    subject: str
    session_id: str
    source_ids: frozenset[str]

    def valid(self):
        return (bool(self.subject) and isinstance(self.subject, str) and bool(self.session_id)
                and isinstance(self.session_id, str) and len(self.subject) <= 300 and len(self.session_id) <= 300
                and "\x00" not in self.subject and "\x00" not in self.session_id
                and isinstance(self.source_ids, frozenset)
                and bool(self.source_ids) and all(isinstance(s, str) and s for s in self.source_ids))


async def verified_owner(verifier, request: Request):
    try:
        value = verifier(request)
        if inspect.isawaitable(value):
            value = await value
        return value if isinstance(value, ExperimentOwner) and value.valid() else None
    except Exception:
        return None


class WebsiteAccess:
    def __init__(self, *, verifier, public_origin: str, source_ids: set[str], secret: bytes):
        parsed = urlsplit(public_origin)
        if (not callable(verifier) or parsed.scheme != "https" or not parsed.netloc
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in {"", "/"}):
            raise ValueError("网站实验需可核验的所有者认证适配器和明确 HTTPS origin")
        self.verifier, self.origin, self.host = verifier, public_origin.rstrip("/"), parsed.netloc
        self.source_ids, self.secret = frozenset(source_ids), secret

    async def owner(self, request):
        if (request.headers.get("host") != self.host
                or request.headers.get("origin", self.origin) != self.origin
                or request.headers.get("sec-fetch-site") == "cross-site"):
            return None
        if request.method not in {"GET", "HEAD"} and request.headers.get("origin") != self.origin:
            return None
        owner = await verified_owner(self.verifier, request)
        if owner is None or not self.source_ids <= owner.source_ids:
            return None
        return owner

    def csrf(self, owner):
        value = owner.subject + "\x00" + owner.session_id
        return hmac.new(self.secret, value.encode(), hashlib.sha256).hexdigest()
