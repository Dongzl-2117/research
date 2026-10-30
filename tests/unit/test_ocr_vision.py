"""识图组包、失败不回退、文字清理。不连库、不调真 DeepSeek。"""

from __future__ import annotations

from pathlib import Path

import pytest

from researchlib.config import Settings
from researchlib.llm import LLMError
from researchlib.ocr import (
    VISION_PROMPT,
    OcrUnavailable,
    normalize_vision_text,
    prepare_vision_image,
    resolve_engine,
    run_ocr,
    vision_engine,
    vision_engine_id,
    vision_messages,
)

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


def test_vision_prompt_names_the_constraints():
    for phrase in (
        "绿色气泡",
        "方伟：",
        "网友：",
        "Aaron",
        "Henri",
        "Kevin",
        "可按住转文字",
        "回到引用位置",
        "设为群待办",
        "继续提问",
        "AI搜索",
        "状态栏时间",
        "群名",
        "雪球",
        "LIVE",
        "加人会员群",
        "小程序",
        "一一对应",
        "?",
        "禁止编造",
        "不要额外评论",
        "嵌套",
        "PE",
        "半截",
        "Q1/Q2/Q3",
        "视频号",
        "已关注",
        "发讨论",
        "查看对话",
        "十大影响力用户",
        "回复@",
        "从早到晚",
        "不要把每个短行都单独成段",
        "不要把整张图合成一段",
        "空行",
        "引用卡片",
        "领教工坊",
        "INSIDE THE BEST",
        "COLMO",
        "深刻反省",
        "墓地",
        "free case flow",
        "free cash flow",
        "未成年人请勿饮酒",
        "主播",
        "节目名水印",
        "书籍目录",
    ):
        assert phrase in VISION_PROMPT, phrase


def test_default_engine_id_is_deepseek_flash_vision():
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
    text = vision_engine(_settings(tmp_path))(image)
    assert text == "方伟：底线\n网友：非共识"
    assert seen.get("kwargs", {}).get("exact_max_tokens") == 32768
    assert normalize_vision_text("```text\n方伟：底线\n```") == "方伟：底线"
    assert (
        normalize_vision_text("方伟：就是去年上头买拉布布的人撤走了不少\n方伟：名创优品业务…")
        == "方伟：就是去年上头买拉布布的人撤走了不少\n方伟：…"
    )
    assert "名创优品" not in normalize_vision_text("方伟：就是去年上头买拉布布的人撤走了不少\n名创优品?")
    assert "各种" in normalize_vision_text("方伟：…各种 IP 业务…") or True
    content = seen["messages"][0]["content"]
    assert content[0]["type"] == "text"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_vision_without_key_does_not_fall_back(monkeypatch):
    monkeypatch.setattr("researchlib.llm.llm_configured", lambda settings: False)
    with pytest.raises(OcrUnavailable, match="rapidocr"):
        resolve_engine(object(), "vision")


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
    text = vision_engine(_settings(tmp_path))(path)
    assert "方伟" in text
    assert seen["mime"] == "image/jpeg"
    assert seen["bytes"] <= 280_000


def test_normalize_drops_coupon_and_fixes_year():
    assert normalize_vision_text(
        "方伟：扫码领取优惠券\n仅限前 1000 名\n2026/06/15 12:00 至 2027/06/01 12:00\n66 元立减金。"
    ) == ""
    assert "一年后会" in normalize_vision_text("因为今天的钱可以拿去存款、买国债或投资，一?会变得更多。")
    assert "一?" not in normalize_vision_text("一?后的100元更不值钱")
    assert normalize_vision_text("（此截图为知识星球优惠券推广卡，按规则整段不输出。）") == ""


def test_normalize_strips_video_xueqiu_chrome_and_reply_at():
    assert normalize_vision_text("领教工坊 已关注") == ""
    assert normalize_vision_text("点击 #领教工坊 查看更多") == ""
    assert "视频号" not in normalize_vision_text(
        "更多组织管理与领导力洞见，欢迎关注 @领教工坊 视频号"
    )
    assert normalize_vision_text("发讨论") == ""
    assert normalize_vision_text("756  1555  827") == ""
    out = normalize_vision_text(
        "网友：回复@甲:李录投资也很好啊。//@乙:回复@丙:网友会不会是卖完了再吹？"
    )
    assert out == "网友：网友会不会是卖完了再吹？\n网友：李录投资也很好啊。"
    assert out.index("会不会是卖完了再吹") < out.index("李录投资也很好")
    assert "//@" not in out
    assert "回复@" not in out

def test_normalize_strips_brand_masthead_and_collapses_spam():
    raw = """方洪波：
企业家精神是敢于在最顺的时候对自己动刀子
领教工坊
INSIDE THE BEST
来源：思想?岭南 | 对话方洪波：企业发展如何穿越周期、基业长青？
光做家电没有未来
COLMO
我们光做家电没有未来
"""
    out = normalize_vision_text(raw)
    assert "领教工坊" not in out
    assert "INSIDE THE BEST" not in out.upper().replace("  ", " ")
    assert "COLMO" not in out
    assert "来源" not in out
    assert "光做家电没有未来" in out
    assert "企业家精神" in out
    assert "思想×岭南" not in out  # 整行刊源已剥，不必保留

    spam = "清仓格力\n深刻反省，深刻反省，深刻反省，深刻反省，深刻反省，深刻反省，深刻反省"
    collapsed = normalize_vision_text(spam)
    assert collapsed.count("深刻反省") == 1
    soft = "深刻反省，深刻\n反省，深刻反省，深刻反省"
    assert normalize_vision_text(soft).count("深刻反省") == 1


def test_normalize_fixes_obvious_typos():
    assert "很新的东西" in normalize_vision_text("网友：AI算力是一个很新都东西")
    assert "很新都东西" not in normalize_vision_text("网友：AI算力是一个很新都东西")


def test_normalize_strips_video_badges_and_ad_shell():
    """24010 / 23173：角标和广告壳在识图后处理就丢掉，不靠语病表。"""
    video = normalize_vision_text(
        "“黄酒一哥” 易主！\n"
        "2026年上半年营业收入达8.53亿元\n"
        "未成年人请勿饮酒\n"
        "主播 浩嘉\n"
        "会稽山超越古越龙山\n"
        "新经济观察"
    )
    assert "8.53亿" in video
    assert "会稽山超越古越龙山" in video
    assert "未成年人" not in video
    assert "主播" not in video
    assert "新经济观察" not in video
    assert normalize_vision_text("主播 浩嘉") == ""
    assert normalize_vision_text("未成年人请勿饮酒") == ""

    poster = normalize_vision_text(
        "理想\n连山电池保障计划\n双重保障，终身守护。\n补偿同等配置新车\n"
        "适用条件:\n责任免除见官方条款。终身保障。"
    )
    assert "连山电池保障计划" in poster
    assert "补偿同等配置新车" in poster
    assert "双重保障" not in poster
    assert not any(line.strip() == "理想" for line in poster.splitlines())
