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
    assert "minmax(0, var(--reading-column)) 320px" in css
    assert ".is-chat-closed" in css
    assert 'html[data-article-chat="closed"]' in css
    assert "/api/v1/ask" in js
    assert "research-article-chat" in js
    assert 'localStorage.getItem("research-article-chat")' in base
