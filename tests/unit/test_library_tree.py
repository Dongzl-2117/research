from researchlib.doctree import filter_library_tree, library_visible


def test_company_and_industry_kept_methodology_hidden():
    assert library_visible("腾讯投资研究")
    assert library_visible("零售研究")
    assert library_visible("公用事业与周期业")
    assert not library_visible("价值投资研究")
    assert not library_visible("巴菲特如何思考")
    assert not library_visible("思维模型")


def test_filter_keeps_only_visible_children_of_home():
    root = {
        "id": 1,
        "title": "主页-Jason 林文丰",
        "children": [
            {"id": 2, "title": "腾讯投资研究", "children": [{"id": 3, "title": "子文档", "children": []}]},
            {"id": 4, "title": "价值投资研究", "children": []},
            {"id": 5, "title": "巴菲特如何思考", "children": []},
            {"id": 6, "title": "白酒研究", "children": []},
        ],
    }
    filtered, orphans = filter_library_tree(root, [{"id": 9, "title": "孤儿", "children": []}])
    titles = [c["title"] for c in filtered["children"]]
    assert titles == ["腾讯投资研究", "白酒研究"]
    assert filtered["children"][0]["children"][0]["title"] == "子文档"
    assert orphans == []
