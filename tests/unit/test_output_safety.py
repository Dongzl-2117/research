"""只用虚构的订阅凭据，覆盖旧 Markdown 清洗和各输出边界。"""

import json

import pytest
from markupsafe import Markup

from researchlib.output_safety import redact_output, redact_subscription_links
from researchlib.snippets import highlight_full, highlight_snippet
from researchlib.textutil import argumentative_excerpt, excerpt, opening_excerpt
from researchlib.web.api import _json
from researchlib.web.mcp import _result


@pytest.mark.parametrize("label", ["rss_link", "rsslink", "RSS-LINK", "rss link"])
def test_historical_feed_metadata_keeps_surrounding_prose(label):
    text = f"开头。{label}: https://archive.example/private?token=synthetic-secret 后文 42。"
    cleaned = redact_subscription_links(text)
    assert "synthetic-secret" not in cleaned
    assert cleaned.startswith("开头。") and cleaned.endswith("后文 42。")


@pytest.mark.parametrize(
    "link",
    [
        "https://stratechery.example/?access_token=synthetic-secret",
        "https://www.stratechery.com/feed/?key=synthetic-secret&amp;format=xml",
        "https://stratechery.com/feed/?%74oken=synthetic-secret",
        "https://stratechery.com/member-rss/synthetic-secret/",
    ],
)
def test_unlabelled_private_feed_is_hidden_in_text_and_html(link):
    for text in (f"Before {link} after", f'<a href="{link}">read</a>'):
        assert "synthetic-secret" not in redact_subscription_links(text)


def test_public_links_and_subscription_discussion_are_untouched():
    text = (
        "Discuss rss_link metadata and subscriptions. "
        "https://stratechery.com/2026/example/?utm_source=rss "
        "https://stratechery.com/feed/ https://example.org/research?key=public-topic"
    )
    assert redact_subscription_links(text) == text


def test_output_boundaries_scrub_nested_snapshots_without_mutating_them():
    text = "rsslink: https://archive.example/private?token=synthetic-secret"
    payload = {"chunks": [{"text": text}], "answer": text, "citations": [{"body": text}], "id": 123}
    clean = redact_output(payload)
    assert clean["id"] == 123 and payload["answer"] == text
    assert "synthetic-secret" not in json.dumps(clean)
    assert "synthetic-secret" not in _json(payload).body.decode()
    mcp = _result(payload)
    assert "synthetic-secret" not in json.dumps(mcp)
    marked = redact_output(Markup(f"<p>{text}</p>"))
    assert isinstance(marked, Markup) and str(marked).startswith("<p>")


def test_redaction_precedes_excerpt_truncation_and_highlight():
    text = "rsslink: https://stratechery.example/feed/?key=synthetic-secret Useful market comparison follows."
    for extract in (
        lambda value: highlight_snippet(value, "Useful"),
        lambda value: highlight_full(value, ["secret", "stratechery"]),
        lambda value: excerpt(value, limit=60),
        lambda value: argumentative_excerpt(value, limit=60),
        lambda value: opening_excerpt(value, limit=60),
    ):
        assert "synthetic-secret" not in str(extract(text))
