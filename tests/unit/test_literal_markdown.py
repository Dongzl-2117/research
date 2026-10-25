"""Small archive-shaped regressions; no private article or network dependency."""

import pytest

from researchlib.boilerplate import EMPTY, clean_markdown
from researchlib.ingest.store import _body_text
from researchlib.render import render_body
from researchlib.textutil import markdown_to_text


@pytest.mark.parametrize(
    "title", ["投资的尽头是实业，实业的尽头是投资", "我的投资审美体系", "The Intelligent Investor", "persistence"]
)
@pytest.mark.parametrize("gap", ["", "\n\n"])
def test_literal_angle_title_survives_ingest_plain_text(title, gap):
    body = f"在<{gap}{title}{gap}>一文中，保留引用。"
    assert title in markdown_to_text(body)
    assert title in _body_text("示例", body, EMPTY)
    assert "一文中，保留引用。" in _body_text("示例", body, EMPTY)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ('<p>正文<span title="x > y">重点</span><br />结尾</p>', "正文 重点 结尾"),
        ("<!-- comment -->普通文字 <em>强调</em>", "普通文字 强调"),
        ('<font color="red">正文</font>', "正文"),
        ("<my-card>正文</my-card>", "正文"),
        ('<my-card state="ready">正文', "正文"),
        ('<svg><text>图</text><circle cx="1" /></svg>', "图"),
        ("普通 < 3，3 > 2，<不是HTML>。", "普通 < 3，3 > 2，<不是HTML>。"),
        (r"\<我的投资审美体系>，&lt;书名&gt;", r"\<我的投资审美体系>，&lt;书名&gt;"),
        ("> 引用 **普通强调**，`代码`。", "引用 普通强调，代码。"),
    ],
)
def test_plain_text_tag_removal_keeps_prose(body, expected):
    assert markdown_to_text(body) == expected


@pytest.mark.parametrize(
    ("body", "strong"),
    [
        ("经营的**“人”**。", "“人”"),
        ("目标就是**“自我增殖”**，即扩张。", "“自我增殖”"),
        ("**“异化”（Alienation）**指的是解释。", "“异化”（Alienation）"),
        ("这里的**「概念」**仍是引文。", "「概念」"),
        ("普通 **强调** 与 *斜体*。", "强调"),
    ],
)
def test_cjk_quoted_strong_in_reader_core(body, strong):
    rendered = str(render_body(clean_markdown(body, EMPTY), {}))
    assert f"<strong>{strong}</strong>" in rendered
    assert "**" not in rendered


def test_cjk_emphasis_does_not_reinterpret_escaped_code_or_attributes():
    body = (
        r'转义的\*\*“人”\*\*。 `代码的**“人”**。`'
        '\n\n```text\n代码的**“人”**。\n```'
        '\n\n<span title="的**“人”**">普通文字</span>'
        '\n\n[链接](https://example.invalid/ "的**“人”**")'
        '\n\n> 引用的**“人”**。'
    )
    rendered = str(render_body(body, {}))
    assert "转义的**“人”**。" in rendered
    assert "<code>代码的**“人”**。</code>" in rendered
    assert 'title="的**“人”**"' in rendered
    assert '<a href="https://example.invalid/" title="的**“人”**">链接</a>' in rendered
    assert "<blockquote>" in rendered
    assert rendered.count("<strong>“人”</strong>") == 1


def test_html_mode_keeps_existing_semantics():
    assert str(render_body('<p>字<strong>“人”</strong></p>', {}, fmt="html")) == (
        '<p>字<strong>“人”</strong></p>'
    )


# Markdown 3.11, as locked by this repository, renders unescaped intraword
# strong emphasis in both main and a3. Literal stars must remain literal when
# escaped or enclosed in code; do not impose an older parser's output on prose.
@pytest.mark.parametrize("body,expected", [
    pytest.param('A**“word”**B', '<p>A<strong>“word”</strong>B</p>', id="english-adjacent-strong"),
    pytest.param('A **“word”**.', '<p>A <strong>“word”</strong>.</p>', id="english-spaced-strong"),
    pytest.param('经营的**“人”**。', '<p>经营的<strong>“人”</strong>。</p>', id="chinese-quoted-strong"),
    pytest.param('甲**“中文”**乙', '<p>甲<strong>“中文”</strong>乙</p>', id="chinese-adjacent-strong"),
    pytest.param(r'A\*\*“word”\*\*B', '<p>A**“word”**B</p>', id="english-literal-stars"),
    pytest.param(r'甲\*\*“中文”\*\*乙', '<p>甲**“中文”**乙</p>', id="chinese-literal-stars"),
    pytest.param('A **word** B', '<p>A <strong>word</strong> B</p>', id="english-normal-strong"),
    pytest.param(
        '普通 **强调** 与 *斜体*。', '<p>普通 <strong>强调</strong> 与 <em>斜体</em>。</p>',
        id="chinese-normal-emphasis",
    ),
    pytest.param('未配对的星号 * 保留', '<p>未配对的星号 * 保留</p>', id="unpaired-literal-star"),
    pytest.param('100 * 1.06 * 40 = 4240', '<p>100 * 1.06 * 40 = 4240</p>', id="spaced-multiplication"),
    pytest.param('100×1.06÷2−1 ≤ 53', '<p>100×1.06÷2−1 ≤ 53</p>', id="math-symbols"),
    pytest.param(r'100\*1.06\*40=4240', '<p>100*1.06*40=4240</p>', id="escaped-multiplication"),
    pytest.param('`A**“word”**B`', '<p><code>A**“word”**B</code></p>', id="english-inline-code"),
    pytest.param('`甲**“中文”**乙`', '<p><code>甲**“中文”**乙</code></p>', id="chinese-inline-code"),
    pytest.param('`100*1.06*40=4240`', '<p><code>100*1.06*40=4240</code></p>', id="math-inline-code"),
    pytest.param(
        '```text\nA**“word”**B\n甲**“中文”**乙\n100*1.06*40=4240\n```',
        '<pre><code class="language-text">A**“word”**B\n甲**“中文”**乙\n100*1.06*40=4240\n</code></pre>',
        id="fenced-literal-code",
    ),
])
def test_reader_emphasis_literal_stars_math_and_code(body, expected):
    assert str(render_body(body, {})) == expected
