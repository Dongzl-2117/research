"""Stratechery 2026 年网页导出单看原文时拼回段落（web_export）。

样本是编的，形状照线上导出：一个文本节点一行，整篇不空行，链接字单独一行。
"""

import markdown

from researchlib.web_export import is_web_export, join_web_export_lines


def _paragraphs(md: str) -> list[str]:
    return join_web_export_lines(md).rstrip("\n").split("\n\n")


def _html(md: str) -> str:
    return markdown.markdown(md, extensions=["extra", "nl2br", "sane_lists"])


def test_only_the_member_rss_header_counts():
    assert is_web_export("# Gadget Day\n\n- source: member-rss\n\nThe gadget is out.\n")
    assert not is_web_export("# Gadget Day\n\n- source: email\n\nThe gadget is out.\n")
    assert not is_web_export("")
    # 只看开头的归档头：正文后面引到这一行字不算。
    assert not is_web_export("The gadget is out.\n" * 200 + "- source: member-rss\n")


def test_link_words_and_the_punctuation_after_them_rejoin():
    md = (
        "I have pointed to this chart\n"
        "before\n"
        ", mostly to note how few people read the footnote under it.\n"
        "The deal closed in\n"
        "March\n"
        ". After that, the regulators moved on.\n"
        "It builds on 2025’s\n"
        "Widget Report\n"
        ", which argued the opposite (\n"
        "and was wrong\n"
        ").\n"
    )
    assert _paragraphs(md) == [
        "I have pointed to this chart before, mostly to note how few people read the footnote under it.",
        "The deal closed in March. After that, the regulators moved on.",
        "It builds on 2025’s Widget Report, which argued the opposite (and was wrong).",
    ]
    assert join_web_export_lines(md).endswith("wrong).\n")


def test_half_sentences_take_the_capitalised_link_words():
    md = (
        "Analysts have argued about this for months, most notably\n"
        "Gadget Research\n"
        ", and the argument is not settled.\n"
        "Ten years ago I wrote that the widget market would consolidate around\n"
        "Widget Corp\n"
        ", and it did.\n"
    )
    assert _paragraphs(md) == [
        "Analysts have argued about this for months, most notably Gadget Research, and the argument is not settled.",
        "Ten years ago I wrote that the widget market would consolidate around Widget Corp, and it did.",
    ]


def test_headings_dates_and_closed_sentences_stay_apart():
    md = (
        "Widget Margins\n"
        "Widget Corp reported its quarter on Tuesday, and the margins were the story.\n"
        "Widget Corp vs. Gadget Inc\n"
        "The two companies took different paths.\n"
        "August 17, 2026\n"
        "Widget Corp said it would raise prices.\n"
        "From\n"
        "Widget Weekly\n"
        ":\n"
        "Widget Corp will raise prices by ten percent next year.\n"
        "He called it “the widget moment.”\n"
        "Gadgets came next.\n"
        "Widget Corp sold more widgets than ever, and that ends the section.[]()\n"
        "The Gadget Question\n"
    )
    assert _paragraphs(md) == [
        "Widget Margins",
        "Widget Corp reported its quarter on Tuesday, and the margins were the story.",
        "Widget Corp vs. Gadget Inc",
        "The two companies took different paths.",
        "August 17, 2026",
        "Widget Corp said it would raise prices.",
        "From Widget Weekly:",
        "Widget Corp will raise prices by ten percent next year.",
        "He called it “the widget moment.”",
        "Gadgets came next.",
        "Widget Corp sold more widgets than ever, and that ends the section.[]()",
        "The Gadget Question",
    ]


def test_interview_speakers_start_their_own_answer():
    md = (
        "JD:\n"
        "Thanks for having me.\n"
        "Interviewer:\n"
        "So what comes after widgets\n"
        "JD:\n"
        "Gadgets, as\n"
        "Widget Weekly\n"
        "put it last year. Jane [Doe]\n"
        "said the same thing.\n"
    )
    assert _paragraphs(md) == [
        "JD: Thanks for having me.",
        "Interviewer: So what comes after widgets",
        "JD: Gadgets, as Widget Weekly put it last year. Jane [Doe] said the same thing.",
    ]


def test_pipe_lists_end_at_their_last_item():
    md = "Topics:\nWidgets\n|\nGadgets\n|\nSprockets\nThe Services Question\nServices grew faster than hardware.\n"
    assert _paragraphs(md) == [
        "Topics: Widgets | Gadgets | Sprockets",
        "The Services Question",
        "Services grew faster than hardware.",
    ]


def test_markdown_blocks_render_as_before():
    table = "| Year | Units |\n| --- | --- |\n| 2025 | 10 |"
    fence = "```\nnot\njoined\n```"
    md = (
        "## Widget Margins\n"
        "margins fell\n"
        "again.\n"
        "\n"
        f"{table}\n"
        "\n"
        "- first\n"
        "- second\n"
        "\n"
        f"{fence}\n"
        "The deal closed in March\n"
        "2026. After that, the regulators moved on.\n"
        "The deal closed in\n"
        "2026. After that, the regulators moved on.\n"
        "![](https://example.com/widget.png)\n"
        "> Quoted\n"
        "lazy line\n"
    )
    paragraphs = _paragraphs(md)
    assert paragraphs == [
        "## Widget Margins",
        "margins fell again.",
        table,
        "- first\n- second",
        fence,
        "The deal closed in March\n2026. After that, the regulators moved on.",
        "The deal closed in 2026. After that, the regulators moved on.",
        "![](https://example.com/widget.png)",
        "> Quoted\nlazy line",
    ]
    html = _html(join_web_export_lines(md))
    assert "<table>" in html and "<li>first</li>" in html and "not\njoined" in html
    # 年份接在上一行后面换行显示，不会变成从 2026 起的编号列表、把年份吞掉。
    assert "<ol>" not in html and "2026. After that" in html
    assert "<blockquote>" in html and "lazy line" in html.split("<blockquote>", 1)[1]


def test_the_export_header_and_blank_lines():
    md = "# Gadget Day\n\n- source: member-rss\n\nThe gadget is\nout\n.\n\nNext paragraph\nhere.\n"
    assert _paragraphs(md) == [
        "# Gadget Day",
        "- source: member-rss",
        "The gadget is out.",
        "Next paragraph here.",
    ]
    assert join_web_export_lines("") == ""
