from researchlib.doctree import ROOT_TITLE, infer_tree


def _row(article_id, name, body, title=None):
    return {
        "id": article_id,
        "external_id": f"docs/{name}",
        "title": title or name.split("__", 1)[0],
        "body_md": body,
        "raw_meta": {"parent": "docs", "filename": name},
    }


def test_first_referrer_is_parent_and_later_cross_links_stay_links():
    rows = [
        _row(
            1,
            "主页-Jason 林文丰__root.md",
            "[零售](零售研究__a.md)\n\n[宏观](宏观__b.md)\n",
            ROOT_TITLE,
        ),
        _row(2, "零售研究__a.md", "[宏观](宏观__b.md)\n\n[渠道](渠道__c.md)\n[改名](旧名__Tokn1234.md)\n"),
        _row(3, "宏观__b.md", "[零售](零售研究__a.md)\n"),
        _row(4, "渠道__c.md", ""),
        _row(5, "新名__Tokn1234.md", ""),
        _row(6, "孤立__z.md", ""),
    ]
    root_id, edges = infer_tree(rows)
    assert root_id == 1
    by_id = {edge["article_id"]: edge for edge in edges}
    assert by_id[1]["parent_id"] is None and by_id[1]["orphan"] is False
    assert by_id[2]["parent_id"] == 1 and by_id[2]["ord"] == 0
    assert by_id[3]["parent_id"] == 1 and by_id[3]["ord"] == 1
    # 宏观里再链到零售，父节点仍是主页。
    assert by_id[4]["parent_id"] == 2
    # 文件名改了，靠 __token 挂到零售下面。
    assert by_id[5]["parent_id"] == 2
    assert by_id[6]["orphan"] is True and by_id[6]["parent_id"] is None


def test_missing_root_does_not_invent_a_tree():
    rows = [_row(1, "零售研究__a.md", "[宏观](宏观__b.md)\n")]
    assert infer_tree(rows) == (None, [])
