"""保护冻结 DOM 计分边界，避免整块标签、上下文缺席或重复片段抬高指标。"""
from evaluate import Page, score_at_k


def _chunk(text, *, evidence=(), required=(), duplicate=None):
    return {
        "text": text,
        "labels": {"q": {
            "grade": 2, "evidence": list(evidence), "required_context": list(required),
            "topic_evidence": {"机制": evidence[0]} if evidence else {},
        }},
        "duplicate_group": duplicate,
    }


def _window(article, center, visible, *, card=0):
    return {"article": article, "center": center, "visible_text": visible, "card": card}


def test_inline_highlight_preserves_literal_evidence_and_drops_link_text():
    page = Page('<blockquote class="item-hit"><p>年度<mark>会费</mark>形成收入 <a href="/articles/1">查看原文</a>'
                '</p><p>上文：某公司的主语。</p></blockquote>')
    node = next(page.root.walk())
    assert "年度会费形成收入" in node.text(omit_links=True)
    assert "查看原文" not in node.text(omit_links=True)


def test_required_context_must_be_visible_in_same_card_and_within_k():
    chunks = {
        "D:0": _chunk("主语在这里"),
        "D:1": _chunk("它带来持续收入", evidence=("带来持续收入",), required=("D:0",)),
    }
    windows = [_window("D", "D:1", "它带来持续收入"), _window("D", "D:0", "主语在这里")]
    query = {"id": "q", "topics": ["机制"]}
    assert score_at_k(windows, query, chunks, 1)["visible_relevant"] == 0
    assert score_at_k(windows, query, chunks, 2)["visible_relevant"] == 1
    windows[1]["card"] = 1
    assert score_at_k(windows, query, chunks, 2)["visible_relevant"] == 0


def test_missing_visible_evidence_gets_no_credit_despite_whole_chunk_label():
    chunks = {"D:0": _chunk("前面是路线。后面是具体机制", evidence=("后面是具体机制",))}
    result = score_at_k([_window("D", "D:0", "前面是路线。…")], {"id": "q", "topics": ["机制"]}, chunks, 5)
    assert result["visible_p"] == 0 and result["topic_count"] == 0
    assert result["returned"] == 1


def test_cross_article_duplicate_gets_only_one_credit_with_fixed_denominator():
    chunks = {f"{doc}:0": _chunk("持续续费形成收入", evidence=("续费形成收入",), duplicate="same")
              for doc in ("A", "B")}
    windows = [_window(doc, f"{doc}:0", "持续续费形成收入", card=i) for i, doc in enumerate(("A", "B"))]
    result = score_at_k(windows, {"id": "q", "topics": ["机制"]}, chunks, 5)
    assert result["visible_p"] == 2 / 5
    assert result["visible_deduplicated_p"] == 1 / 5
    assert result["duplicate_count"] == 1 and result["duplicate_rate"] == 1 / 2
