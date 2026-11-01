"""在输出边界遮住个人订阅地址，兼容尚未清理的旧块和回答快照；不改库。"""

from __future__ import annotations

import html
import re
from urllib.parse import parse_qsl, urlsplit

from markupsafe import Markup

_LABELLED_FEED = re.compile(
    r"\brss[_ -]?link[ \t]*[:：][ \t]*<?https?://[^\s<>\"'\])]+>?", re.I
)
_URL = re.compile(r"https?://[^\s<>\"'\])]+", re.I)
_CREDENTIAL_KEYS = frozenset({"access_token", "token", "key", "auth", "secret", "signature", "password"})
_HIDDEN = "[个人订阅地址已隐藏]"


def _private_feed(url: str) -> bool:
    try:
        parsed = urlsplit(html.unescape(url))
        host = (parsed.hostname or "").lower().removeprefix("www.")
        # .example 只给合成回归用；普通文章地址、公共 RSS、别的站点链接不动。
        if host not in {"stratechery.com", "stratechery.example"}:
            return False
        keys = {key.lower() for key, _value in parse_qsl(parsed.query)}
        return bool(keys & _CREDENTIAL_KEYS) or parsed.path.lower().startswith("/member-rss/")
    except ValueError:
        return False


def redact_subscription_links(text: str) -> str:
    """归档头可被旧版去 Markdown 变成 rsslink；历史片段也可能只剩地址。"""
    text = _LABELLED_FEED.sub(_HIDDEN, text)
    text = _URL.sub(lambda match: _HIDDEN if _private_feed(match.group()) else match.group(), text)
    # HTML 里的失效订阅链接不保留成一个会跳 404 的相对地址。
    return re.sub(r'''\s+href\s*=\s*(["'])\[个人订阅地址已隐藏\]\1''', "", text, flags=re.I)


def redact_output(value):
    """JSON、MCP 和模板共用；保留 Markup 的已转义身份，原对象不变。"""
    if isinstance(value, str):
        cleaned = redact_subscription_links(value)
        return Markup(cleaned) if isinstance(value, Markup) else cleaned
    if isinstance(value, dict):
        return {key: redact_output(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_output(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_output(item) for item in value)
    return value
