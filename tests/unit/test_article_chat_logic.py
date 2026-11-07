from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_reading_column_stays_680_and_chat_collapses_before_it_would_overflow():
    css = (ROOT / "src/researchlib/web/static/site.css").read_text(encoding="utf-8")
    js = (ROOT / "src/researchlib/web/static/article-chat.js").read_text(encoding="utf-8")
    base = (ROOT / "src/researchlib/web/templates/base.html").read_text(encoding="utf-8")
    assert "--reading-column: 680px" in css
    assert "font-size: 17px" in css
    assert "letter-spacing: 0" in css
    assert "line-height: 1.65" in css
    assert ".article-chat" in css
    assert "translateY(110%)" in css
    assert "@media (min-width: 1320px)" in css
    assert "minmax(0, var(--reading-column)) 8px minmax(260px, 1fr)" in css
    # 外扩阴影会盖住 8px 分割条，字又贴着栏边。白边改留在栏内，宽图仍铺满 680。
    assert "box-shadow: 0 0 0 16px" not in css
    assert "padding-left: 20px" in css and "padding-right: 20px" in css
    # 图按原尺寸、最多 680：不定死宽度，否则 48px 的小图标也放大成 680 宽。
    image_rule = css.split(".shell:has(.reader-grid) .reader-grid .prose img {", 1)[1].split("}", 1)[0]
    assert "max-width: calc(100% + 40px)" in image_rule and "translateX(-50%)" in image_rule
    assert not any(line.strip().startswith("width:") for line in image_rule.splitlines())
    wide = css.split("@media (min-width: 1320px)", 1)[1]
    assert "padding: 0 0 4px 8px" not in css
    assert "padding: 12px 20px 16px 24px" in wide
    assert "data-chat-ratio" in css
    assert "col-resize" in css
    assert ".is-chat-closed" in css
    assert 'html[data-article-chat="closed"]' in css
    assert "/api/v1/ask" in js
    assert "research-article-chat" in js
    assert "research-article-chat-ratio" in js
    assert "pointerdown" in js and "dblclick" in js and "ArrowLeft" in js
    assert 'localStorage.getItem("research-article-chat")' in base
    assert "research-article-chat-ratio" in base
    assert "align-items: baseline" in css
    assert "emptyText" not in js
    assert "先看这篇" not in js
    assert "主要看这篇文章" not in js
    template = (ROOT / "src/researchlib/web/templates/article.html").read_text(encoding="utf-8")
    assert "先看这篇" not in template
    assert "chat-chip" not in template
    assert "chat-hint" not in template
    assert "placeholder=" not in template
    assert "Enter 发送" not in template
    assert 'aria-describedby="article-chat-mode"' in template
    assert "整个资料库 · 单轮" in template
