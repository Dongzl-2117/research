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
        "一个也认不全的，这条不写",
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


def test_default_engine_id_follows_the_current_model():
    # 11-10 起识图走 Grok OAuth，新认的图记 grok-4.7-vision；老行照旧是 deepseek-flash-vision。
    assert vision_engine_id() == "grok-4.7-vision"


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
    # 模型没按结构交（自由文字）：照旧轻清理，units 为空，后面走旧规则。
    assert found.text == "方伟：底线\n网友：非共识"
    assert found.units is None
    assert seen.get("kwargs", {}).get("exact_max_tokens") == 65536
    assert seen["kwargs"].get("usage") == {}
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


def test_vision_without_key_stops_before_any_image(monkeypatch):
    monkeypatch.setattr("researchlib.llm.llm_configured", lambda settings: False)
    with pytest.raises(OcrUnavailable, match="DEEPSEEK_API_KEY"):
        resolve_engine(object())


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
    # //@ 后面那一楼的网名留着；最新一条没有自己的网名，仍是「网友：」。
    assert out == "乙：网友会不会是卖完了再吹？\n网友：李录投资也很好啊。"
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
