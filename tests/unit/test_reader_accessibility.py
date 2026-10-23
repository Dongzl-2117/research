from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from researchlib.render import image_anchor, inline_image_ocr, normalize_admonitions, render_body
from researchlib.textutil import strip_duplicate_title


def test_admonition_markers_render_as_labels_without_rewriting_code():
    body = "> [!NOTE]\n> Keep this body.\n\n```text\n> [!WARNING]\n```\n\nLiteral [!NOTE] stays."
    normalized = normalize_admonitions(body)
    assert "> **说明**" in normalized
    assert "> [!WARNING]" in normalized
    assert "Literal [!NOTE] stays." in normalized
    html = str(render_body(body, {}))
    assert "<strong>说明</strong>" in html
    assert "Keep this body." in html


def test_long_or_truncated_heading_keeps_all_text_as_body():
    heading = "这是一段完整正文。" * 50
    assert strip_duplicate_title(f"# {heading}\n\n尾段", heading[:300]) == heading + "\n\n尾段"
    assert strip_duplicate_title("# 短标题\n正文", "短标题") == "正文"
    assert strip_duplicate_title("# 一段有更多内容的标题\n正文", "一段有更多…").startswith("一段有更多内容的标题\n\n")
    assert strip_duplicate_title("# 不同标题\n正文", "页面标题") == "# 不同标题\n正文"
    assert strip_duplicate_title("# Alternative\n正文", "A") == "# Alternative\n正文"


def test_local_image_alt_is_preserved_and_missing_image_is_readable():
    image = "f" * 64
    html = str(render_body('<img src="chart.png" alt="营收趋势 &amp; 利润">',
                           {"chart.png": f"/media/x/{image}.png"}, "html"))
    assert 'alt="营收趋势 &amp; 利润"' in html
    assert 'loading="lazy"' in html
    absent = str(render_body('<img src="missing.png" alt="原始图表">', {}, "html"))
    assert "原始图表（图片未收录）" in absent
    assert "<img" not in absent


def test_reference_chart_has_neutral_readable_fallback_and_accessible_original():
    digest = "a" * 64
    image = f"/media/source/{digest}.png"
    body = str(render_body('![营收与利润](chart.png)', {"chart.png": image}))
    raw = "平台  B段运费规则  平台现金补贴  流量激励  商家自由度\n拼多多  全额补贴B段  <untrusted>"
    page = inline_image_ocr(body, {digest: raw})
    assert "图表/资料摘录（非对话，作者未确认）" in page
    assert "全额补贴B段" in page and "&lt;untrusted&gt;" in page
    assert "<untrusted>" not in page and "方伟：" not in page and "网友：" not in page
    assert page.count(f'id="{image_anchor(digest)}"') == 1
    assert f'href="{image}"' in page
    assert 'aria-label="原图 1：营收与利润"' in page


def test_commerce_ui_stays_out_of_readable_fallback():
    digest = "b" * 64
    body = str(render_body('![商品原图](shop.png)', {"shop.png": f"/media/source/{digest}.png"}))
    raw = "精选  商品  分类  评价  小店介绍\n微信小店\n￥5.99  点赞补贴价  去参与\n上滑继续看视频"
    page = inline_image_ocr(body, {digest: raw})
    assert "原图 1" in page
    assert "点赞补贴价" not in page and "小店介绍" not in page


def _env():
    path = Path(__file__).resolve().parents[2] / "src/researchlib/web/templates"
    env = Environment(loader=FileSystemLoader(path), autoescape=select_autoescape(["html"]))
    env.filters.update(answer_html=lambda text: text, highlight_snippet=lambda text, *args: text,
                       highlight_full=lambda text, *args: text)
    return env


def test_saved_ask_warning_precedes_answer_and_exact_scope_is_preserved():
    html = _env().get_template("ask.html").render(
        title="问库", sources=[], authors=["方伟"], llm_ready=True, question="问题",
        filters={"author": "方伟", "source": "", "from": "", "to": ""},
        saved_source_ids=["fangwei-mp", "fangwei-zsxq"], saved_scope_label="方伟的两个来源",
        saved_scope_unknown=True, company=None, article=None, tree=None,
        result={"question": "问题", "answer": "ANSWER-MARKER", "status": "complete", "citations": [],
                "checks": [{"sentence": "尚未核实", "refs": [], "problems": ["出处不支持"]}]},
    )
    assert html.index('aria-label="引用核验警告"') < html.index("ANSWER-MARKER")
    assert 'name="saved_scope_present" value="1"' in html
    assert html.count('name="saved_sources"') == 2
    assert 'name="saved_scope_author" value="方伟"' in html
    assert "原有资料范围无法完整核实" in html


def test_zoom_has_keyboard_dialog_and_restores_focus():
    html = _env().get_template("base.html").render(sources=[], title="Read")
    assert 'role="dialog" aria-modal="true" aria-label="查看原图"' in html
    assert 'class="img-zoom-close"' in html
    assert 'event.key === "Enter" || event.key === " "' in html
    assert "zoomReturn.focus()" in html
    assert "zoomImg.alt = img.alt" in html
    assert 'event.key === "Tab"' in html


def test_history_company_and_empty_tree_states_explain_missing_evidence():
    env = _env()
    history = env.get_template("revisions.html").render(article={"id": 1, "title": "标题"},
                                                       unverified_history=2, entries=[], sources=[])
    assert "2 条历史版本缺少可核实的作者来源" in history
    assert "暂无可核实的历史改动" in history
    tree = env.get_template("tree.html").render(source={"name": "飞书", "id": "feishu"},
                                              article=None, root=None, orphans=[], sources=[])
    assert "这个来源暂时没有可浏览的文档" in tree
    assert "research-doc-tree" not in tree


def test_mixed_sample_banner_does_not_call_all_real_rows_demo():
    html = _env().get_template("base.html").render(title="Read", sources=[], sample_banner="mixed")
    assert "当前库里包含示例数据" in html
    assert "当前库里全是示例数据" not in html
    all_sample = _env().get_template("base.html").render(title="Read", sources=[], sample_banner="all")
    assert "当前库里全是示例数据" in all_sample


def test_quarantined_owner_tail_has_visible_escaped_notice():
    html = _env().get_template("article.html").render(
        article={"id": 1, "title": "标题"}, source={"id": "s", "name": "来源", "author": "作者", "timeline": True},
        identity_notice="旧评论后的归档片段暂未确认归属 <untrusted>", sources=[], body_html="OWNER-BODY-MARKER",
        body_language="original",
    )
    assert 'role="status"' in html
    assert "旧评论后的归档片段暂未确认归属 &lt;untrusted&gt;" in html
    assert html.index("旧评论后的归档片段") < html.index("OWNER-BODY-MARKER")
