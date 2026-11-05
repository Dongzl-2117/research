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
