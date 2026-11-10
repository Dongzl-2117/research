"""长截图切段认：哪些图要切、切在哪、每段压完字多大、几段的结果怎么接回一张。不连库、不调真 DeepSeek。

11-09 抽查：整张压到长边 1920 像素，1080×7237 的长截图送出去宽只剩 286，错了 6 处、改了意思。
"""

from __future__ import annotations

import base64
import json
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from researchlib.config import Settings
from researchlib.llm import LLMError
from researchlib.ocr import (
    VISION_TILE_OVERLAP,
    VISION_TILE_RATIO,
    Recognition,
    _outcome,
    _row_noise,
    describe_report,
    main,
    needs_tiles,
    prepare_vision_image,
    prepare_vision_images,
    tile_count,
    tile_spans,
    vision_engine,
)

ROOT = Path(__file__).resolve().parents[2]
LINE = 40  # 一行字的高
GAP = 24  # 行与行之间的空白


def _settings(tmp_path) -> Settings:
    return Settings.from_env(
        {
            "DATA_DIR": str(tmp_path / "data"),
            "PUBLIC_BASE_URL": "https://investment.zldong.com",
            "SOURCES_FILE": str(ROOT / "config" / "sources.yaml"),
            "DISPLAY_TIMEZONE": "Asia/Shanghai",
            "DEEPSEEK_API_KEY": "sk-ocr-tiles-test",
        }
    )


def _article_shot(width: int, height: int) -> Image.Image:
    """白底上一行行「字」：每行是一排黑白相间的竖条，行与行之间空 GAP 像素，每 6 行多空一段（分段）。"""
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    y, row = 30, 0
    while y + LINE < height - 30:
        for x in range(40, width - 40, 18):
            draw.rectangle((x, y, x + 9, y + LINE - 1), fill="black")
        y += LINE + GAP + (GAP * 2 if row % 6 == 5 else 0)
        row += 1
    return image


def _png(image: Image.Image) -> bytes:
    buf = BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def test_only_shots_much_longer_than_a_phone_screen_are_cut():
    assert not needs_tiles(1080, 2341)  # 普通手机截图
    assert not needs_tiles(1290, 2796)
    assert not needs_tiles(1080, 3053)  # 高宽比 2.8，整张认
    assert not needs_tiles(500, 1800)  # 不比 VISION_MAX_SIDE 高，没被压
    assert needs_tiles(1080, 3300)
    assert needs_tiles(1080, 7237)
    assert tile_count(1080, 2341) == 1
    assert tile_count(1080, 3300) == 2
    assert tile_count(1080, 7237) == 4
    assert tile_count(1080, 60000) == 12  # 再长也只切这么多段，每段跟着变高


@pytest.mark.parametrize("height", [3300, 4321, 7237, 10647])
def test_tiles_cover_the_shot_with_overlap_and_stay_phone_sized(height):
    width = 1080
    spans = tile_spans(width, height)
    assert len(spans) == tile_count(width, height)
    assert spans[0][0] == 0 and spans[-1][1] == height
    for (_top, bottom), (next_top, _next_bottom) in zip(spans, spans[1:], strict=False):
        assert bottom - next_top == round(VISION_TILE_OVERLAP * width)
    assert all(bottom - top <= VISION_TILE_RATIO * width for top, bottom in spans)


def test_cuts_land_between_lines_and_tiles_keep_text_as_large_as_a_phone_shot():
    image = _article_shot(1080, 7237)
    noise = _row_noise(image)
    spans = tile_spans(image.width, image.height, noise)
    width = image.width
    assert len(spans) == 4
    for top, bottom in spans:
        assert bottom - top <= 2.2 * width
        for row in (top, bottom - 1):
            if 0 < row < image.height - 1:
                assert noise(row, row + 1) == [0.0], row  # 切口那一行是整行空白，没压着字
    for (_top, bottom), (next_top, _next_bottom) in zip(spans, spans[1:], strict=False):
        assert bottom - next_top >= 0.2 * width  # 挪过切口，两段还重叠一截
    pieces = prepare_vision_images(_png(image), "image/png")
    assert len(pieces) == 4
    for (top, bottom), (payload, mime) in zip(spans, pieces, strict=True):
        assert mime == "image/jpeg"
        sent = Image.open(BytesIO(payload))
        # 和手机一屏的截图压完一样大：普通截图送出去宽是原图的 0.82，字密、超过字节上限再缩一次是 0.7。
        assert sent.width >= 0.7 * width
        assert abs(sent.height / sent.width - (bottom - top) / width) < 0.01


def test_cut_prefers_the_widest_blank_band_nearby():
    """附近有段与段之间的宽空白，就不切在行间的窄空白上。"""
    width, height = 1000, 4000
    # 切成 3 段，第二段的上边原位在 1200：1190 处一条 4 行的窄空白，1260 处一条 30 行的宽空白，别处都压着字。
    blank = set(range(1188, 1192)) | set(range(1245, 1275))

    def noise(top: int, bottom: int) -> list[float]:
        return [0.0 if row in blank else 40.0 for row in range(top, bottom)]

    spans = tile_spans(width, height, noise)
    assert spans[1][0] == 1259
    assert spans[0][1] == 1600  # 附近都压着字，切口留在原位，靠两段重叠不丢字


def test_blank_rows_inside_a_dark_picture_count_as_blank():
    """深色配图左右是白边：图里没字的那几行只有两条边，切口挑得到；压着图里白字的行挑不到。"""
    image = Image.new("RGB", (1080, 400), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((100, 0, 980, 399), fill="black")
    for y in (40, 200, 330):
        for x in range(150, 930, 16):
            draw.rectangle((x, y, x + 7, y + 30), fill="white")
    noise = _row_noise(image)(0, 400)
    assert max(noise[100:190]) < 1 < min(noise[40:70])


def test_normal_screenshot_is_sent_whole_as_before():
    raw = _png(_article_shot(1080, 2341))
    assert prepare_vision_images(raw, "image/png") == [prepare_vision_image(raw, "image/png")]
    broken = b"\x89PNG\r\n\x1a\nnot-an-image"
    assert prepare_vision_images(broken, "image/png") == [prepare_vision_image(broken, "image/png")]


def _reply(*blocks: dict, kind: str = "chat") -> str:
    return json.dumps({"kind": kind, "blocks": list(blocks)}, ensure_ascii=False)


def _msg(text: str, speaker: str = "王小明", **extra) -> dict:
    return {"type": "message", "speaker": speaker, "self": False, "text": text, **extra}


def test_long_shot_is_read_tile_by_tile_and_joined(tmp_path, monkeypatch):
    path = tmp_path / "long.png"
    path.write_bytes(_png(_article_shot(1080, 4000)))
    replies = [
        _reply(_msg("一季度收入增长两成", cut="top"), _msg("毛利率35%，比去年高")),
        _reply(_msg("毛利率35%，比去年高"), _msg("先看现金流", "李四")),
        _reply(_msg("先看现金流", "李四"), _msg("经营现金流连续三年为正", "李四", cut="bottom")),
    ]
    seen: list[tuple[int, int]] = []

    def fake_chat(settings, messages, **kwargs):
        url = messages[0]["content"][1]["image_url"]["url"]
        seen.append(Image.open(BytesIO(base64.b64decode(url.split(",", 1)[1]))).size)
        kwargs["usage"]["prompt"] = kwargs["usage"].get("prompt", 0) + 2000
        kwargs["usage"]["completion"] = kwargs["usage"].get("completion", 0) + 9000
        return replies[len(seen) - 1], "deepseek-flash"

    monkeypatch.setattr("researchlib.llm.chat", fake_chat)
    found = vision_engine(_settings(tmp_path))(path)
    assert len(seen) == tile_count(1080, 4000) == 3
    assert all(width >= 0.8 * 1080 for width, _height in seen)
    assert found.error == ""
    assert found.units["tiles"] == 3
    assert [(block["text"], block.get("cut", "")) for block in found.units["blocks"]] == [
        ("一季度收入增长两成", "top"),
        ("毛利率35%，比去年高", ""),
        ("先看现金流", ""),
        ("经营现金流连续三年为正", "bottom"),
    ]
    assert found.text == "王小明：一季度收入增长两成\n毛利率35%，比去年高\n\n李四：先看现金流\n经营现金流连续三年为正"
    assert found.usage == {"prompt": 6000, "completion": 27000}  # 三段的用量加在一起


def test_a_tile_that_cannot_be_read_fails_the_whole_shot(tmp_path, monkeypatch):
    """一段读不出来，整张记读不出来：只写一部分的字比旧字还糟，重认时旧字留着。"""
    path = tmp_path / "long.png"
    path.write_bytes(_png(_article_shot(1080, 4000)))
    calls = []

    def fake_chat(settings, messages, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            return _reply(_msg("毛利率35%，比去年高")), "deepseek-flash"
        return '{"kind": "chat", "blocks": [{"type": "message", "text": "半截', "deepseek-flash"

    monkeypatch.setattr("researchlib.llm.chat", fake_chat)
    found = vision_engine(_settings(tmp_path))(path)
    assert found.text == "" and found.units is None and found.error
    assert len(calls) == 3  # 第一段认成；第二段读不出来，再要一次还不行就停，第三段不再花钱


def test_a_tile_that_errors_fails_the_whole_shot(tmp_path, monkeypatch):
    path = tmp_path / "long.png"
    path.write_bytes(_png(_article_shot(1080, 4000)))
    calls = []

    def fake_chat(settings, messages, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise LLMError("DeepSeek 接口返回 500：busy")
        return _reply(_msg("毛利率35%，比去年高")), "deepseek-flash"

    monkeypatch.setattr("researchlib.llm.chat", fake_chat)
    with pytest.raises(LLMError):
        vision_engine(_settings(tmp_path))(path)


def test_a_tile_answered_in_plain_text_is_kept_as_a_paragraph(tmp_path, monkeypatch):
    path = tmp_path / "long.png"
    path.write_bytes(_png(_article_shot(1080, 3300)))
    replies = [_reply(_msg("毛利率35%，比去年高")), "经营现金流连续三年为正"]

    def fake_chat(settings, messages, **kwargs):
        return replies.pop(0), "deepseek-flash"

    monkeypatch.setattr("researchlib.llm.chat", fake_chat)
    found = vision_engine(_settings(tmp_path))(path)
    assert found.units["tiles"] == 2
    assert found.text == "王小明：毛利率35%，比去年高\n\n经营现金流连续三年为正"


def test_tall_option_is_not_mixed_with_named_articles():
    with pytest.raises(SystemExit):
        main(["--tall", "--article", "1"])


def test_report_counts_long_shots_and_their_tiles():
    class Done:
        def __init__(self, result):
            self._result = result

        def result(self):
            return self._result, None, 1.0

    report = {"structured": 0, "plain": 0, "invalid": 0, "failed": 0, "metered": 0, "tokens": {}, "last_error": None}
    tiled = Recognition("王小明：毛利率35%", {"v": 1, "kind": "chat", "blocks": [], "tiles": 4}, {"prompt": 8000})
    whole = Recognition("王小明：毛利率35%", {"v": 1, "kind": "chat", "blocks": []}, {"prompt": 2000})
    for result in (tiled, whole):
        _outcome("deepseek-flash-vision", Path("x.png"), Done(result), report)
    assert (report["structured"], report["tiled"], report["tiles"]) == (2, 1, 4)
    assert "长截图 1 张切成 4 段认（每段的花费和一张普通截图差不多）" in describe_report(report)


def test_rejoin_joins_stored_tiles_again_without_calling_the_model(monkeypatch):
    """接法改了以后 research-ocr --rejoin：按库里存的每段结果重接，写回 units 和 text，重排引用它的文章。不调模型。"""
    from researchlib.ocr import rejoin_stored
    from researchlib.ocr_units import format_units, join_tiles

    def fail_chat(*_args, **_kwargs):
        raise AssertionError("重接不该调模型")

    monkeypatch.setattr("researchlib.llm.chat", fail_chat)
    first = {"v": 1, "kind": "text", "blocks": [{"type": "text", "text": "春季发布会\n软件与智能驾驶\n6月15日 16:30"}]}
    second = {
        "v": 1,
        "kind": "text",
        "blocks": [
            {"type": "text", "text": "春季发布会\n软件与智能驾驶"},
            {"type": "text", "text": "6月15日 16:30"},
            {"type": "text", "text": "届时将公布全新的系统"},
        ],
    }
    fresh = join_tiles([first, second])
    # 以前的接法把海报写了两遍：存着的 blocks 是旧的，parts 是当时每段认出来的
    stale = {**fresh, "blocks": [*first["blocks"], *second["blocks"]]}
    stored = [
        {"content_hash": "aa" * 32, "text": format_units(stale), "units": stale},
        {"content_hash": "ab" * 32, "text": format_units(fresh), "units": fresh},
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

        def execute(self, sql, params=None):
            if "FROM image_texts" in sql:
                assert "units -> 'parts' IS NOT NULL" in sql
                return _Rows(stored)
            if sql.startswith("UPDATE image_texts"):
                self.updates.append(params)
                return _Rows([])
            if "article_images" in sql:
                assert params == (["aa" * 32],)
                return _Rows([{"article_id": 7}])
            raise AssertionError(sql)

        def commit(self):
            self.commits += 1

    refreshed = []
    monkeypatch.setattr("researchlib.ocr.refresh_article_ocr", lambda _conn, article_id: refreshed.append(article_id))
    monkeypatch.setattr("researchlib.chunks.replace_ocr_chunks", lambda _conn, _article_id: 1)
    conn = _Conn()
    assert rejoin_stored(conn, dry_run=True) == {"matched": 2, "changed": 1, "articles": 0, "dry_run": True}
    assert conn.updates == [] and conn.commits == 0
    report = rejoin_stored(conn)
    assert report == {"matched": 2, "changed": 1, "articles": 1, "dry_run": False}
    [(units, text, digest)] = conn.updates
    assert digest == "aa" * 32 and units.obj == fresh
    assert text == format_units(fresh) and text.count("春季发布会") == 1
    assert refreshed == [7] and conn.commits == 1


def test_rejoin_is_not_mixed_with_recognising():
    for extra in (["--tall"], ["--redo"], ["--article", "1"], ["--relayout"]):
        assert main(["--rejoin", *extra]) == 2
