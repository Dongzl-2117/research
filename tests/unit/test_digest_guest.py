"""星球读者帖的摘要读全文和图片文字，不写成段永平没说话。"""

from types import SimpleNamespace

from researchlib import digest as digest_mod

_PLACEHOLDER = "（没有段永平本人的话）"
_OCR = "图里写着短期不宜提价，这是读者自己的判断，不是别人代写的。"


class _Conn:
    def __init__(self):
        self.writes: list[tuple] = []

    def execute(self, sql, params=None):
        self.writes.append((sql, params))
        return self

    def commit(self):
        return None


def _row(**extra):
    row = {
        "id": 7,
        "source_id": "fangwei-zsxq",
        "title": "读者帖",
        "published_at": None,
        "body_text": "正文很短。",
        "content_hash": "hash",
        "raw_meta": {"poster_role": "guest", "poster_name": "某读者"},
    }
    row.update(extra)
    return row


def test_guest_digest_keeps_body_and_ocr(monkeypatch):
    seen: list[str] = []

    def chunks(_conn, _article_id):
        return [("", "正文很短。")]

    def ocr(_conn, _article_id):
        return _OCR, "ocrhash"

    def chat(_settings, messages, **_kwargs):
        seen.append(messages[1]["content"])
        return '{"summary": "读者认为短期不宜提价", "points": ["图里的判断"], "tags": ["茅台"]}', "grok-test"

    monkeypatch.setattr(digest_mod, "original_chunks", chunks)
    monkeypatch.setattr(digest_mod, "article_ocr", ocr)
    monkeypatch.setattr(digest_mod, "chat", chat)
    conn = _Conn()
    data = digest_mod.digest_article(conn, SimpleNamespace(display_timezone="Asia/Shanghai"), _row(), "方伟")
    assert seen, "读者帖应该交给模型，而不是直接写成空回答"
    prompt = seen[0]
    assert _PLACEHOLDER not in data["summary"]
    assert _PLACEHOLDER not in prompt
    assert "段永平本人写的段落" not in prompt
    assert "正文很短" in prompt
    assert "短期不宜提价" in prompt
    assert "【图片里的字】" in prompt
    saved = conn.writes[0][1]
    assert _PLACEHOLDER not in saved[3]


def test_duan_empty_voice_still_skips_the_model(monkeypatch):
    def chat(*_args, **_kwargs):
        raise AssertionError("段永平本人没有字，不该再问模型")

    monkeypatch.setattr(digest_mod, "original_chunks", lambda _conn, _article_id: [])
    monkeypatch.setattr(digest_mod, "article_ocr", lambda _conn, _article_id: (_OCR, "ocrhash"))
    monkeypatch.setattr(digest_mod, "chat", chat)
    data = digest_mod.digest_article(
        _Conn(),
        SimpleNamespace(display_timezone="Asia/Shanghai"),
        _row(
            source_id="duanyongping-blog",
            title="空回答",
            body_text="读者问了很长一段。",
            raw_meta={"author_voice": "段永平"},
        ),
        "段永平",
    )
    assert data["summary"] == _PLACEHOLDER
