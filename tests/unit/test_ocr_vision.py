"""识图组包、失败不回退、没按结构交的字怎么存。不连库、不调真 DeepSeek。"""

from __future__ import annotations

from pathlib import Path

import pytest

from researchlib.config import Settings
from researchlib.llm import LLMError
from researchlib.ocr import (
    VISION_PROMPT,
    VISION_PROMPT_VERSION,
    OcrUnavailable,
    prepare_vision_image,
    resolve_engine,
    run_ocr,
    vision_engine,
    vision_engine_id,
    vision_messages,
)
from researchlib.ocr_units import has_dialogue, plain_units

ROOT = Path(__file__).resolve().parents[2]
SECRET = "sk-ocr-vision-must-not-leak"


def _settings(tmp_path) -> Settings:
    return Settings.from_env(
        {
            "DATA_DIR": str(tmp_path / "data"),
            "PUBLIC_BASE_URL": "https://investment.zldong.com",
            "SOURCES_FILE": str(ROOT / "config" / "sources.yaml"),
            "DISPLAY_TIMEZONE": "Asia/Shanghai",
            "DEEPSEEK_API_KEY": SECRET,
        }
    )


def test_vision_prompt_asks_for_structure_not_app_rules():
    """提示词是一份通用说明：交 JSON，说话人、截图主人、引用、截断各自成字段，界面字按一条原则不写。

    以前这里对着几十个按案例写的短语（雪球加群卡、领教工坊、墓地……），每来一种版式就补一条。
    现在不认 App：遇到新版式改通用说明，不往提示词里加案例。
    第 2 版（11-08 拿 30 张旧图对比后）补的也是通用说明：看得清的照抄、看不清的写 ? 不编、帖子的发布日期、后画的标注。
    """
    for phrase in (
        "只输出一个 JSON 对象",
        '"kind"',
        '"blocks"',
        '"type": "message"',
        '"speaker"',
        '"self"',
        '"quote"',
        '"cut": "top" 或 "bottom"',
        '"type": "text"',
        '"type": "table"',
        "只有聊天界面里截图的人自己发的写 true",
        "是那个帖子的作者，不一定是截图的人",
        "看不到名字写空串",
        "只写图里的内容，界面一概不写",
        "读者转述这张图时会不会说这句话",
        "被回复的那条更早",
        "只写看得到的字，不要补全",
        "一个也认不全的",
        "illegible",
        "不要因为标了 cut 就删掉",
        "不要把看不全的笔画补成一句通顺的话",
        "看不清的字写 ?",
        "不要用图里别处出现过的词补",
        "也不要拿你自己知道的说法",
        "宁可多写几个 ?",
        "作者的错别字、重复的字、不通顺的句子、少见的名字和拼写都照抄",
        "帖子、推文、文章署的发布日期",
        '"marks"',
        "版面本来就有的下划线、虚线",
        '"small": true',
        "引用框归和它挨得最近、在同一侧对齐的那条消息",
        "图上没标的数不要估",
        "blocks 写 []",
    ):
        assert phrase in VISION_PROMPT, phrase
    # 11-08 对比里模型拿这句当按意思改字的许可（作者笔误「大吵预期」被改成「大超预期」），换成只在笔画模糊时挑。
    assert "上下文只容得下一个词时，写那个词" not in VISION_PROMPT
    for case_rule in ("雪球专属交流群", "领教工坊", "墓地", "方伟：", "网友：", "十年知己"):
        assert case_rule not in VISION_PROMPT, case_rule
    # 分清 cut 和 illegible 不升版本：按第 3 版认过的图不因此再进重认队列。
    assert VISION_PROMPT_VERSION == 3


def test_default_engine_id_follows_the_current_model(monkeypatch):
    # 新认的图记当前模型。换提供方只改以后写入的 engine，不靠它决定要不要重认。
    monkeypatch.delenv("RESEARCH_LLM_PROVIDER", raising=False)
    assert vision_engine_id() == "grok-4.7-vision"
    monkeypatch.setenv("RESEARCH_LLM_PROVIDER", "deepseek")
    assert vision_engine_id() == "deepseek-flash-vision"


def test_vision_messages_are_text_plus_image_url():
    messages = vision_messages(b"\x89PNG\r\n", "image/png")
    assert messages[0]["role"] == "user"
    content = messages[0]["content"]
    assert [part["type"] for part in content] == ["text", "image_url"]
    assert content[0]["text"] == VISION_PROMPT
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_vision_recognize_posts_multimodal_messages(tmp_path, monkeypatch):
    image = tmp_path / "shot.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    seen = {}

    def fake_chat(settings, messages, **kwargs):
        seen["messages"] = messages
        seen["kwargs"] = kwargs
        return "```\n方伟：底线\n网友：非共识\n```", "deepseek-flash"

    monkeypatch.setattr("researchlib.llm.chat", fake_chat)
    found = vision_engine(_settings(tmp_path))(image)
    # 模型没按结构交（自由文字）：去掉代码块围栏，字照原样当成一段文字存，不按冒号前的字猜谁说的。
    # 行首的「方伟：」不是识图结构标的说话人，排版时加引号，不当成作者的话（审核 C5）。
    assert found.text == "「方伟：底线」\n网友：非共识"
    assert found.units == plain_units("方伟：底线\n网友：非共识")
    assert not has_dialogue(found.units)
    assert seen.get("kwargs", {}).get("exact_max_tokens") == 65536
    assert seen["kwargs"].get("usage") == {}
    content = seen["messages"][0]["content"]
    assert content[0]["type"] == "text"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_vision_without_key_stops_before_any_image(monkeypatch):
    monkeypatch.delenv("RESEARCH_LLM_PROVIDER", raising=False)
    monkeypatch.setattr("researchlib.llm.llm_configured", lambda settings: False)
    with pytest.raises(OcrUnavailable, match="Grok OAuth"):
        resolve_engine(object())

    class _DeepSeek:
        llm_provider = "deepseek"

    with pytest.raises(OcrUnavailable, match="DEEPSEEK_API_KEY"):
        resolve_engine(_DeepSeek())


def test_http_error_is_explicit_empty_not_dirty_text(tmp_path, monkeypatch):
    image_dir = tmp_path / "images"
    image_dir.mkdir()
    (image_dir / "a.png").write_bytes(b"png")
    monkeypatch.setattr(
        "researchlib.ocr.pending_images",
        lambda *args, **kwargs: [{"content_hash": "ab" * 32, "storage_path": "a.png"}],
    )

    def boom(_path):
        raise LLMError("DeepSeek 接口返回 500：busy")

    class Conn:
        def __init__(self):
            self.writes = []

        def execute(self, sql, params=None):
            compact = " ".join(sql.split())
            if compact.startswith("INSERT INTO image_texts"):
                self.writes.append(params)

            class Result:
                def fetchall(self_inner):
                    return []

                def fetchone(self_inner):
                    return None

            return Result()

        def commit(self):
            return None

    conn = Conn()
    report = run_ocr(conn, tmp_path, 1, boom, engine="deepseek-flash-vision")
    assert report["failed"] == 1
    assert report["with_text"] == 0
    assert conn.writes[0][1] == "deepseek-flash-vision:error"
    assert conn.writes[0][2] == ""


def test_nightly_uses_app_vision_not_ocr_profile():
    text = (ROOT / "scripts" / "nightly.sh").read_text()
    assert "--profile ocr" not in text
    assert "research-ocr --engine vision" in text


def test_prepare_vision_image_shrinks_large_jpeg():
    raw = (Path(__file__).resolve().parents[1] / "fixtures" / "ocr-vision-large.jpg").read_bytes()
    assert len(raw) > 120_000
    out, mime = prepare_vision_image(raw, "image/jpeg")
    assert mime == "image/jpeg"
    assert len(out) <= 280_000
    assert len(out) < len(raw)


def test_vision_recognize_compresses_before_chat(tmp_path, monkeypatch):
    raw = (Path(__file__).resolve().parents[1] / "fixtures" / "ocr-vision-large.jpg").read_bytes()
    path = tmp_path / "big.jpg"
    path.write_bytes(raw)
    seen = {}

    def fake_chat(settings, messages, **kwargs):
        content = messages[0]["content"]
        url = content[1]["image_url"]["url"]
        b64 = url.split(",", 1)[1]
        import base64

        seen["bytes"] = len(base64.b64decode(b64))
        seen["mime"] = url.split(";")[0].split(":")[1]
        return "方伟：压缩后仍可读\n网友：好", "deepseek-flash"

    monkeypatch.setattr("researchlib.llm.chat", fake_chat)
    try:
        import PIL  # noqa: F401
    except ImportError:
        monkeypatch.setattr(
            "researchlib.ocr.prepare_vision_image",
            lambda image_bytes, mime: (image_bytes[:80_000], "image/jpeg"),
        )
    text = vision_engine(_settings(tmp_path))(path).text
    assert "方伟" in text
    assert seen["mime"] == "image/jpeg"
    assert seen["bytes"] <= 280_000


def test_relayout_rewrites_stored_text_from_units_without_calling_the_model(monkeypatch):
    """#297 把看得清的 cut 整段换成了占位句。units 还在，重排写回 text，不改版本、不调模型。

    ``--written-to 03:33:02`` 含这一整秒：03:33:02.350005 在里面，03:33:03 不在。
    """
    from datetime import timedelta

    from researchlib.ocr import parse_written_at, relayout_stored, relayout_until
    from researchlib.ocr_units import format_units

    units = {
        "v": 1,
        "kind": "chat",
        "blocks": [{
            "type": "message",
            "speaker": "豆包",
            "self": False,
            "text": "2016: Standard $9.99",
            "cut": "bottom",
        }],
    }
    start = parse_written_at("2026-11-10 03:28:41")
    end = parse_written_at("2026-11-10 03:33:02")
    assert start.tzinfo is not None and start.utcoffset().total_seconds() == 8 * 3600
    assert relayout_until(end) == end + timedelta(seconds=1)
    last = end + timedelta(microseconds=350005)
    stored = [
        {"content_hash": "aa" * 32, "text": "旧", "units": units, "created_at": start - timedelta(microseconds=1)},
        {"content_hash": "ab" * 32, "text": "（末尾一条被遮挡，无法辨认）", "units": units, "created_at": last},
        {"content_hash": "ac" * 32, "text": "旧", "units": units, "created_at": end + timedelta(seconds=1)},
    ]

    class _Rows:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return self._rows

    class _Conn:
        def __init__(self):
            self.updates = []
            self.commits = 0
            self.bounds = None

        def execute(self, sql, params=None):
            if "FROM image_texts" in sql:
                assert "created_at < %s" in sql
                _engine, begun, until = params
                self.bounds = (begun, until)
                picked = [
                    row for row in stored
                    if begun <= row["created_at"] < until
                ]
                return _Rows(picked)
            if sql.startswith("UPDATE image_texts"):
                self.updates.append(params)
                return _Rows([])
            if "article_images" in sql:
                return _Rows([{"article_id": 7}])
            raise AssertionError(sql)

        def commit(self):
            self.commits += 1

    refreshed = []

    def _refresh(_conn, article_id):
        refreshed.append(article_id)
        return "h"

    monkeypatch.setattr("researchlib.ocr.refresh_article_ocr", _refresh)
    monkeypatch.setattr("researchlib.chunks.replace_ocr_chunks", lambda conn, article_id: 1)
    conn = _Conn()
    report = relayout_stored(conn, engine="grok-4.7-vision", written_from=start, written_to=end)
    assert conn.bounds == (start, end + timedelta(seconds=1))
    assert report == {"matched": 1, "changed": 1, "articles": 1, "dry_run": False}
    assert conn.updates == [(format_units(units), "ab" * 32)]
    assert "2016" in conn.updates[0][0]
    assert "无法辨认" not in conn.updates[0][0]
    assert refreshed == [7] and conn.commits == 1
    assert ocr_main_refuses_relayout_without_a_window()


def ocr_main_refuses_relayout_without_a_window() -> bool:
    from researchlib.ocr import main

    return main(["--relayout"]) == 2
