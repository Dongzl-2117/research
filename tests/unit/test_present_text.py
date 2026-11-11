"""阅读页展示层：对话标签统一、明显错字、星球问答标题。"""

import re
from pathlib import Path

from researchlib.boilerplate import clean_body, search_text
from researchlib.mdlinks import prepare_body_html
from researchlib.present_text import (
    normalize_dialogue_labels,
    plain_qa_markdown,
    polish_display_text,
    present_article_text,
    present_zsxq_qa,
    rejoin_broken_paragraphs,
)
from researchlib.render import render_body

_CSS = Path(__file__).resolve().parents[2] / "src" / "researchlib" / "web" / "static" / "site.css"

# 形状对齐文章 24006：标题是回答的第一句，正文另有截断的「# 问：」，
# 再加「## 提问（…）」「## 回答（…）」。名字用虚构的，星主仍写方伟。
_QA_24006 = """# AI算力是一个很新都东西

# 问：星主好，想请教目前腾讯这笔投入以后怎么收回来，回报能不能超过基本的ROIC(…

- 作者：王小明
- 时间：2026-10-27 09:55 (UTC+8)
- 类型：q&a
- 点赞 14 · 评论 0 · 阅读人数 475
- topic_id：`55188` · 原链接：https://wx.zsxq.com/topic/55188
- 原始数据： raw/topics/55188.json

## 提问（王小明 → 方伟Rey）

星主好，想请教目前腾讯这笔投入以后怎么收回来，回报能不能超过基本的ROIC(暂按10%)？谢谢

## 回答（方伟Rey）

AI算力是一个很新都东西，其实你看中美科技巨头也在被迫回答这个问题。

而且，他们的商业模式决定了他们会把算力留给自己尝试
"""
_QA_TITLE = "AI算力是一个很新都东西"


def test_legacy_comment_labels_become_fangwei_wangyou():
    raw = "## 评论里的回复\n\n读者问：奈飞怎么样？\n\n作者答：运营杠杆还在。\n\n作者补充：再看一眼广告。\n"
    out = normalize_dialogue_labels(raw)
    assert "网友：奈飞怎么样？" in out
    assert "方伟：运营杠杆还在。" in out
    assert "方伟：再看一眼广告。" in out
    assert "读者问" not in out and "作者答" not in out and "作者补充" not in out


def test_polish_fixes_obvious_typos_only():
    assert polish_display_text("AI算力是一个很新都东西") == "AI算力是一个很新的东西"
    assert polish_display_text("存在运用杠杆的好处") == "存在运营杠杆的好处"
    # 口语不动
    assert polish_display_text("我觉得还行吧") == "我觉得还行吧"


def test_youtube_netflix_zsxq_fixture_reads_as_one_dialogue_skin():
    """23999：正文短句 + 评论里的回复旧标签。问答标题转换不能把这一节吃掉。"""
    body = """YouTube的护城河类型是平台网络效应，Netflix只是规模效应，前者比后者强

## 评论里的回复

读者问：奈飞生意其实很不错的，每年内容成本固定200亿。

作者答：这里面很多人误会的应该是觉得YouTube的成本比Netflix低，其实不然，YouTube是可变成本低，它是一半要分给创作者，Netflix是存在运用杠杆的好处。
"""
    html = str(render_body(body, {}))
    assert "读者问" not in html and "作者答" not in html
    assert '<span class="who">网友：</span>奈飞生意其实很不错的' in html
    assert '<span class="who">方伟：</span>这里面很多人误会的' in html
    assert "方伟：YouTube的护城河" not in html
    assert "存在运营杠杆的好处" in html
    assert "<h2>评论里的回复</h2>" in html
    assert "提问（" not in html and "回答（" not in html
    # 评论区不是另一套 zsxq-comment 列表，而是与正文同一段落对话体
    assert "zsxq-comment-author" not in html
    assert html.count('class="who"') == 2


def test_present_article_text_composes_label_and_typo():
    text = "读者问：很新都东西\n\n作者答：存在运用杠杆"
    assert present_article_text(text) == "网友：很新的东西\n\n方伟：存在运营杠杆"


def test_24006_qa_headings_render_as_dialogue_paragraphs():
    """24006 这种问答导出：不要 h1/h2「问： / 提问 / 回答」，改成和 OCR 一样的 .who。"""
    cleaned = clean_body(_QA_24006)
    assert "topic_id" not in cleaned and "王小明" in cleaned
    html = prepare_body_html(cleaned, _QA_TITLE, {}, None, None)
    assert "<h1" not in html and "<h2" not in html
    assert "提问" not in html and "回答（" not in html and "问：" not in html
    assert "王小明" not in html and "方伟Rey" not in html
    assert '<span class="who">网友：</span>星主好，想请教目前腾讯这笔投入以后怎么收回来' in html
    assert "暂按10%" in html
    assert "ROIC(…" not in html
    assert '<span class="who">方伟：</span>AI算力是一个很新的东西，其实你看中美科技巨头也在被迫回答这个问题。' in html
    assert "很新都东西" not in html
    assert "而且，他们的商业模式决定了他们会把算力留给自己尝试" in html
    assert "方伟：而且" not in html
    assert html.count('class="who"') == 2
    assert present_zsxq_qa(present_zsxq_qa(cleaned)) == present_zsxq_qa(cleaned)


def test_plain_qa_markdown_keeps_the_asker_name_out():
    """接口给的原文和检索文字：问答小标题只留「提问」「回答」，归档头里的作者（提问的网友）不要，别的行照旧。"""
    plain = plain_qa_markdown(_QA_24006)
    assert "王小明" not in plain and "方伟Rey" not in plain
    expected = (
        _QA_24006.replace("- 作者：王小明\n", "")
        .replace("## 提问（王小明 → 方伟Rey）", "## 提问")
        .replace("## 回答（方伟Rey）", "## 回答")
    )
    assert plain == expected
    assert plain_qa_markdown(plain) == plain
    text = search_text(clean_body(_QA_24006), _QA_TITLE)
    assert "王小明" not in text and "方伟Rey" not in text
    assert "\n提问\n" in text and "\n回答\n" in text
    # 提问以后的「作者：」是帖子的话，不动。
    quoted = _QA_24006.replace("而且，", "- 作者：李四\n\n而且，")
    assert "- 作者：李四" in plain_qa_markdown(quoted)
    # 不是问答导出（只有一种小标题、在代码块里）原样返回，归档头也不动。
    notes = "# 读书笔记\n\n- 作者：王小明\n\n## 回答（续）\n\n上回没说完的。\n"
    assert plain_qa_markdown(notes) == notes
    fenced = "- 作者：王小明\n\n```\n## 提问（王小明 → 方伟Rey）\n\n## 回答（方伟Rey）\n```\n"
    assert plain_qa_markdown(fenced) == fenced


def test_qa_keeps_comment_replies_and_host_comment_list():
    """问答正文换成对话之后，评论里的回复和「评论（N）」列表都还在。"""
    body = """# 问：为什么供应链做不出差异

## 提问（王小明 → 方伟Rey）

为什么供应链做不出差异

## 回答（方伟Rey）

下图

![](shot.jpg)

## 评论里的回复

读者问：差异化是不是只能做在服务上？

作者答：服务又跟人力成本绑在一起。

## 评论（1）

- **方伟Rey**（2026-01-09 15:07）
  再补一句。
"""
    html = str(render_body(body, {"shot.jpg": "/media/fangwei-zsxq/ab/shot.jpg"}))
    assert "<h1" not in html
    assert "提问（" not in html and "回答（" not in html
    assert '<span class="who">网友：</span>为什么供应链做不出差异' in html
    assert '<span class="who">方伟：</span>下图' in html
    assert 'src="/media/fangwei-zsxq/ab/shot.jpg"' in html
    assert "<h2>评论里的回复</h2>" in html
    assert '<span class="who">网友：</span>差异化是不是只能做在服务上？' in html
    assert '<span class="who">方伟：</span>服务又跟人力成本绑在一起。' in html
    assert "读者问" not in html and "作者答" not in html
    assert 'class="zsxq-comment-author">方伟Rey<' in html
    assert "再补一句" in html
    assert html.index("评论里的回复") < html.index("zsxq-comments")


def test_title_wen_and_longer_duplicate_wen_both_drop():
    """标题本身是截短的「问：」，正文里还有一行更长的「问：」重复提问。"""
    body = """# 问：Rey，你会用期权吗

# 问：Rey，你会用期权吗，比直接卖 put

## 提问（王小明 → 方伟Rey）

Rey，你会用期权吗，比直接卖 put

## 回答（方伟Rey）

可买可不买的时候再看。
"""
    html = prepare_body_html(body, "问：Rey，你会用期权吗", {}, None, None)
    assert "<h1" not in html and "<h2" not in html
    assert html.count("比直接卖 put") == 1
    assert '<span class="who">网友：</span>Rey，你会用期权吗，比直接卖 put' in html
    assert '<span class="who">方伟：</span>可买可不买的时候再看。' in html


def test_wen_heading_that_is_not_the_question_stays():
    body = """# 问：先看这张表

## 提问（王小明 → 方伟Rey）

利润率以后怎么走？

## 回答（方伟Rey）

还要看投放。
"""
    html = str(render_body(body, {}))
    assert "<h2>问：先看这张表</h2>" in html
    assert '<span class="who">网友：</span>利润率以后怎么走？' in html
    assert '<span class="who">方伟：</span>还要看投放。' in html
    assert "提问（" not in html


def test_plain_headings_and_fenced_qa_are_left_alone():
    prose = "他在回答里提到提问。\n\n## 回答的框架\n\n先看回报。\n"
    assert present_zsxq_qa(prose) == prose
    bare = "## 提问\n\n怎么看？\n\n## 回答\n\n还行。\n"
    assert present_zsxq_qa(bare) == bare
    fenced = (
        "说明\n\n```\n## 提问（王小明 → 方伟Rey）\n\n怎么看？\n\n## 回答（方伟Rey）\n\n还行。\n```\n"
    )
    assert present_zsxq_qa(fenced) == fenced
    crlf = "提问和回答都出现了，但不是标题。\r\n下一行。\r\n"
    assert present_zsxq_qa(crlf) == crlf


def test_reading_column_is_narrow_and_images_fit_it():
    """文章栏接近公众号宽度。图不超出这一栏；竖长截图按手机屏宽（26em，约 440px）显示，
    不再铺满 680px（字有正文两倍大），也不回到以前按 480px 限高压成两百来像素宽。"""
    css = _CSS.read_text(encoding="utf-8")
    assert "--reading-column: 680px" in css
    column = css.split(".reader-grid {", 1)[1].split("}", 1)[0]
    assert "max-width: var(--reading-column)" in column
    assert "width: 100%" in column
    assert "min-width: 0" in column
    images = css.split(".prose img, .prose video {", 1)[1].split("}", 1)[0]
    assert "max-width: 100%" in images
    assert "width: auto" in images
    assert "720px" not in images
    assert "56vh" not in images
    assert "480px" not in images
    assert "max-height: 1800px" in images
    assert "cursor: zoom-in" in css.split(".prose img {", 1)[1].split("}", 1)[0]
    tall = css.split('.prose img[data-shape="tall"] {', 1)[1].split("}", 1)[0]
    assert "max-width: min(100%, 26em)" in tall and "max-height: none" in tall
    wide_tall = css.split('.shell:has(.doc-read) .doc-read .prose img[data-shape="tall"] {', 1)[1].split("}", 1)[0]
    assert "max-width: min(100%, 26em)" in wide_tall
    reading = css.split(".reader-grid .prose,\n.doc-read .prose {", 1)[1].split("}", 1)[0]
    assert "font-size: 17px" in reading
    assert "line-height: 1.65" in reading
    assert "letter-spacing: 0" in reading
    gap = css.split(".reader-grid .prose:not(.bilingual-en) p + p,", 1)[1].split("}", 1)[0]
    assert "margin-top: 1.15em" in gap
    title = css.split(".reader h1 {", 1)[1].split("}", 1)[0]
    assert "font-size: 22px" in title
    assert "letter-spacing: 0" in title
    assert "text-wrap: balance" in title
    assert ".shell:has(.reader) .sidebar" in css
    assert ".reader h1 { font-size: 32px" not in css
    assert ".reader h1 { font-size: 36px" not in css
    assert css.count(".reader h1 {") == 1
    english = css.split(".bilingual-en.prose {", 1)[1].split("}", 1)[0]
    assert "letter-spacing: 0" in english
    assert "line-height: 1.65" in english


def test_heading_serif_falls_back_to_the_body_font():
    """标题的宋体在没装宋体的设备上退回正文字体，不退到浏览器默认的 serif。

    iPhone 的 Safari 给中文页面的 serif 用 Times，Times 没有中文：标题里的中文落到苹方，
    数字和英文却是 Times，一行里两种字体。
    """
    root = _CSS.read_text(encoding="utf-8").split(":root {", 1)[1].split("}", 1)[0]
    stack = root.split("--font-serif:", 1)[1].split(";", 1)[0]
    assert stack.split(",")[0].strip() == '"Songti SC"'
    assert stack.split(",")[-1].strip() == "var(--font-sans)"
    assert "serif" not in [name.strip() for name in stack.split(",")]


def test_single_newline_lines_stay_on_their_own_lines():
    """单换行分开的几行（星球帖里的「1、」「2、」「3、」，Stratechery 网页导出的一行一段）各占一行。

    以前有一条 ``.prose br + br { display: none; }`` 想收掉连着的空行，可兄弟选择器不看中间的文字：
    「第一行<br>第二行<br>第三行」里第二个 <br> 也被藏掉，从第三行起全粘成一段。
    """
    html = render_body("今天聊三件事：\n1、拼多多。\n2、茅台。\n3、腾讯。\n", {})
    assert html.count("<br") == 3
    css = re.sub(r"/\*.*?\*/", "", _CSS.read_text(encoding="utf-8"), flags=re.S)
    for selector, body in re.findall(r"([^{}]*)\{([^{}]*)\}", css):
        if "display: none" in body:
            assert not re.search(r"\bbr\s*[+~]\s*br\b", selector), selector


def test_ocr_blocks_inherit_prose_font_without_grey_box():
    css = _CSS.read_text(encoding="utf-8")
    rules = css.split(".prose .ocr-reading {", 1)[1].split(".prose .ocr-links {", 1)[0]
    assert "font-size: 15px" not in rules
    assert "var(--bg-sunk)" not in rules
    assert "background: var(--surface)" not in rules
    assert "font-size: inherit" in rules
    assert "font-weight: inherit" in rules
    assert "line-height: inherit" in rules
    assert "color: inherit" in rules
    links = css.split(".prose .ocr-links a {", 1)[1].split("}", 1)[0]
    assert "font-size: 13px" in links


def test_prose_links_use_blue_but_ocr_reading_keeps_text_color():
    """正文外链回到蓝链；识图转写整段都是链接，仍跟周围文字同色。"""
    css = _CSS.read_text(encoding="utf-8")
    root = css.split(":root {", 1)[1].split("}", 1)[0]
    dark = css.split('[data-theme="dark"] {', 1)[1].split("}", 1)[0]
    assert "--link:" in root and "--link-visited:" in root
    assert "--link:" in dark and "--link-visited:" in dark
    rule = css.split(".prose a {", 1)[1].split("}", 1)[0]
    visited = css.split(".prose a:visited {", 1)[1].split("}", 1)[0]
    hover = css.split(".prose a:hover {", 1)[1].split("}", 1)[0]
    assert "color: var(--link)" in rule
    assert "text-decoration: underline" in rule
    assert "currentColor" in rule
    assert "var(--accent)" not in rule
    assert "color: var(--link-visited)" in visited
    assert "var(--accent)" not in hover
    assert "currentColor" in hover
    # 识图转写的字本身是链到原图的链接：不刷蓝，靠淡下划线辨认。
    ocr = css.split(".prose .ocr-reading a {", 1)[1].split("}", 1)[0]
    assert "color: inherit" in ocr
    assert "text-decoration: underline" in ocr
    ocr_visited = css.split(".prose .ocr-reading a:visited {", 1)[1].split("}", 1)[0]
    assert "color: inherit" in ocr_visited
    ocr_hover = css.split(".prose .ocr-reading a:hover {", 1)[1].split("}", 1)[0]
    assert "color: inherit" in ocr_hover
    # 表格整张也是链接：比上面那条更具体，仍不画下划线。
    table = css.split(".prose .ocr-reading a.ocr-table-link {", 1)[1].split("}", 1)[0]
    assert "color: inherit" in table
    assert "text-decoration: none" in table
    # 「原图 N」仍是小一号的控件，不跟正文句子抢颜色。
    original = css.split(".prose .ocr-links a {", 1)[1].split("}", 1)[0]
    assert "var(--ink-3)" in original
    original_hover = css.split(".prose .ocr-links a:hover {", 1)[1].split("}", 1)[0]
    assert "var(--accent)" not in original_hover


# 文章 24040 的形状：图后面硬换行把逗号留在下一行，页码「1」是「1988 年」被拆开的首位。
_BROKEN_24040 = """吃的量就跟自己掏钱买的时候差不多了

![](shot.jpg)
，巧克力是会腻的，绝大多数东西都会腻。

但可乐没有味觉记忆。”

老巴能够洞察到可乐“不会腻”这一点是非常厉害的，能高频消费可乐的人是越来越多的）

，另一点厉害的洞察是“可乐=快乐”。

机会成本是 8.85% 的时候，那就是 PE≈6.78 才算非常便宜。

1

988 年标普 500 TTM PE 大致就在 10～12 倍区间。

1、AI 短剧能赚钱吗？
"""


def test_rejoin_moves_leading_punctuation_and_splits_year_back():
    """24040：阅读栏不要以逗号起段，也不要留下拆开的页码数字。"""
    out = rejoin_broken_paragraphs(_BROKEN_24040)
    assert out == rejoin_broken_paragraphs(out)
    assert "\n，" not in out and not out.startswith("，")
    assert "，巧克力" not in out
    assert "越来越多的），另一点厉害的洞察是" in out
    assert "差不多了，\n\n![](shot.jpg)\n巧克力是会腻的" in out
    assert "\n1\n" not in out
    assert "1988 年标普 500 TTM PE" in out
    assert "1、AI 短剧能赚钱吗？" in out
    html = str(render_body(out, {"shot.jpg": "/media/x/ab/" + "ab" * 32 + ".jpg"}))
    assert "，巧克力" not in html
    assert ">，" not in html and ">。" not in html and ">？" not in html
    assert ">1</p>" not in html
    assert "1988 年标普" in html
    # 图仍紧挨后面那一句（一个硬换行），不把图单独撑成上下都空一段。
    assert "<br" in html and "巧克力是会腻的" in html


def test_rejoin_keeps_lone_numbers_that_are_not_split_years():
    # 公众号单独一段的小节号「01」「02」：拼不成年份就照原样留着，不当页码删。
    sections = "引子说完了。\n\n01\n\n先说生意模式\n\n这一节说完了！\n\n02\n\n再说估值\n"
    assert rejoin_broken_paragraphs(sections) == sections
    lone = "才算非常便宜。\n\n12\n\n本来还想展开讲讲长江电力。\n"
    assert rejoin_broken_paragraphs(lone) == lone
    kept = "答案是\n\n1\n\n就这样"
    assert rejoin_broken_paragraphs(kept) == kept
    # 下一段以数字开头、又拼不成年份：不动，免得把页码和「2026 年」粘成「22026 年」。
    kept_year = "收句。\n\n2\n\n2026 年还这样。\n"
    assert rejoin_broken_paragraphs(kept_year) == kept_year
    quote = "他说：\n\n“\n\n大众的判断有时候跟事实差很远。\n"
    assert rejoin_broken_paragraphs(quote) == "他说：\n\n“大众的判断有时候跟事实差很远。\n"
    period = "付出过高的价格\n\n。\n\n下一句。"
    assert rejoin_broken_paragraphs(period) == "付出过高的价格。\n\n下一句。"
    # 代码块挡住，里面的逗号和单独的数字都不动，后面的正文也不跨过去接。
    fenced = "收句\n\n```\n，这行在代码里\n1\n```\n\n，这行要接上。\n"
    assert rejoin_broken_paragraphs(fenced) == fenced
    untouched = "第一段说完了。\n\n第二段另起，不该并进去。\n"
    assert rejoin_broken_paragraphs(untouched) == untouched
    # 中文里的半角逗号照样接。
    mixed = "能高频消费可乐的人是越来越多的\n, 另一点厉害的洞察是“可乐=快乐”。\n"
    assert rejoin_broken_paragraphs(mixed) == "能高频消费可乐的人是越来越多的,另一点厉害的洞察是“可乐=快乐”。\n"


# Stratechery 2026 年网页导出的形状（合成）：一个文本节点一行，链接字单独一行，后面的标点和下文另起一行。
_LONG_TAIL = " ".join(
    f"Sentence {word} keeps the paragraph going with enough words to push the group past the split threshold."
    for word in ("one", "two", "three", "four", "five", "six", "seven", "eight", "nine")
)
_WEB_EXPORT_EN = (
    "Platforms win when they own the default, and that has been true for every era of computing.\n"
    "I have pointed to this chart\n"
    "before\n"
    ", mostly to note how few people read the footnote under it.\n"
    "The deal closed in\n"
    "March\n"
    f". After that, the regulators moved on. {_LONG_TAIL}\n"
    "Read the earlier piece in\n"
    "the archive\n"
    ".\n"
    "“\n"
    "Quoted words stay where the export put them.”\n"
)


def test_rejoin_leaves_english_lines_alone():
    """英文的行不接：接过去丢掉标点后的空格（「before,mostly」「March.After」）。"""
    assert rejoin_broken_paragraphs(_WEB_EXPORT_EN) == _WEB_EXPORT_EN
    html = str(render_body(_WEB_EXPORT_EN, {}))
    assert "before,mostly" not in html and "March.After" not in html


def test_english_web_export_keeps_the_segments_older_translations_used():
    """22245：接了行，长段按句切时「March.After」少一刀，11-04 以前按原样译的稿子就比原文多段，对照页退成两栏。"""
    from researchlib.render import canonical_body_html
    from researchlib.translate.display import article_pairs
    from researchlib.translate.segments import iter_segments

    original = canonical_body_html(_WEB_EXPORT_EN, "Synthetic export", {}, None)
    leaves = [leaf.html for leaf in iter_segments(original)]
    assert len(leaves) == 15
    # 对照页里这一句照样连成一句，标点后面有空格。
    assert leaves[1].startswith("<p>I have pointed to this chart before, mostly")
    assert leaves[2] == "<p>The deal closed in March.</p>"
    translated = "".join(f"<p>第{name}句的译文。</p>" for name in "甲乙丙丁戊己庚辛壬癸子丑寅卯辰")
    page = article_pairs(original, translated)
    assert "对不上" not in page.notice
    assert page.pairs and all(pair.en and pair.zh for pair in page.pairs)
