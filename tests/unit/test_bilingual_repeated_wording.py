"""导出里写了两遍的引文、列表项：对照页收掉一份时，中文同一句不再出现两次。

Stratechery 的归档导出先把整条引文、整个列表项写成一行纯文字，再带着格式分段写一遍
（见 ``researchlib.nested_copies``）。两份各自译，同一句英文常译成只差几个字的两句。
对照页收掉一份以后，以前按「整句一字不差」判断哪些中文是新的，换了说法的同一句、
后面几段已经单独显示的译文，都被挂到留下的那一对后面，读者看到同一句中文两遍。
现在收掉的是纯文字那份（``test_bilingual_nested_copies``），它的中文只挂页面上还没说过的句子。
"""

from researchlib.render import render_body
from researchlib.translate import provenance as pr
from researchlib.translate.display import _novel_zh, article_pairs
from researchlib.translate.segments import splice_segments

_P1 = (
    "The keynote opened with a demo of the new storage service, which most people expected to stay limited to "
    "one platform. It is easy to skip over this part, but it is hard to overstate how important the announcement "
    "was for the company. The company used to treat open-source projects as a threat; now it builds on them."
)
_P2 = (
    "More broadly, the cloud division has supported both systems for some time, and only it also runs the older "
    "server line. This matters a great deal to established enterprises with existing servers in their own "
    "buildings. (Update: the rival cloud does run the older line)"
)
_T1 = (
    '<p>主题演讲以<a href="https://example.invalid/demo">新存储服务的演示</a>开场，大多数人原以为它会一直只支持一个平台。'
    "这一部分很容易被略过。但这项发布对公司有多重要，怎么说都不为过。公司过去把开源项目当成威胁，现在却在它们之上搭建产品。</p>"
)
_T2 = (
    "<p>更广泛地说，云部门支持这两套系统已经有一段时间了，而且只有它还运行旧的服务器产品线。"
    '这对那些在自己楼里拥有<a href="https://example.invalid/servers">现有服务器</a>的成熟企业来说非常重要。'
    "（更新：对手的云确实也运行旧产品线）</p>"
)
# 整行那份是另一次译的：句子切法、个别用词和带格式的两段不一样。
_T_LINE = (
    "<p>主题演讲以新存储服务的演示开场，大多数人原本以为它会一直只支持一个平台。"
    "这一部分很容易被略过，但这项发布对公司有多重要，怎么说都不为过。"
    "公司过去把开源项目当成威胁，现在却在它们之上搭建产品。"
    "更广泛地说，云部门支持这两套系统已经有一段时间，而且只有它还运行旧的服务器产品线。"
    "这对那些在自己楼里拥有现有服务器的成熟企业来说非常重要。（更新：对手的云确实也运行旧产品线）</p>"
)
_CLOSING_ZH = "<p>最后一段讲定价，和上面无关，留在原处。</p>"


def _list_item_copy(line_zh: str = _T_LINE, first_zh: str = _T1):
    """列表里一条两段的项：先写成一行，再带链接分两段写一遍。同一列表里另有一条单段的项。"""
    p1 = _P1.replace("a demo of the new storage service", "[a demo of the new storage service](https://example.invalid/demo)")
    p2 = _P2.replace("existing servers", "[existing servers](https://example.invalid/servers)")
    md = (
        "- A single paragraph point about the hardware strategy stays as one list item.\n"
        f"- {_P1} {_P2}\n\n{p1}\n\n{p2}\n\n"
        "An unrelated closing paragraph talks about pricing and stays where it is.\n"
    )
    source = str(render_body(md, {}))
    manifest = pr.source_manifest(source)
    target = splice_segments(manifest["source_snapshot"], {
        0: "<p>关于硬件战略的单段要点，仍是一个列表项。</p>", 1: line_zh, 2: first_zh, 3: _T2, 4: _CLOSING_ZH,
    })
    return article_pairs(source, target, provenance=pr.receipt(manifest, target))


def _shown_zh(view) -> str:
    return "".join(pair.zh or "" for pair in view.pairs)


def test_list_item_line_copy_does_not_repeat_chinese_of_the_formatted_paragraphs():
    view = _list_item_copy()
    assert view.correspondence == "mapped" and not view.notice
    shown = _shown_zh(view)
    # 整行那份收掉了；第一段后面不再挂它的译文，第二段的译文只在第二段出现一次。
    assert sum("The keynote opened" in pair.en for pair in view.pairs) == 1
    assert shown.count("更广泛地说") == 1
    assert shown.count("对手的云确实也运行旧产品线") == 1
    assert shown.count("大多数人原") == 1
    first = next(pair for pair in view.pairs if "The keynote opened" in pair.en)
    # 留下的是带链接的那份，套回列表项里。
    assert first.en.startswith("<ul><li><p>") and 'href="https://example.invalid/demo"' in first.en
    assert first.zh == f"<ul><li>{_T1}</li></ul>"


def test_sentence_missing_from_the_kept_translation_is_still_shown():
    """留下的那段漏译了一句，收掉的那份里有：这句仍挂回去，只挂这一句，外壳不乱。"""
    missing = "公司过去把开源项目当成威胁，现在却在它们之上搭建产品。"
    view = _list_item_copy(first_zh=_T1.replace(missing, ""))
    assert view.correspondence == "mapped" and not view.notice
    first = next(pair for pair in view.pairs if "The keynote opened" in pair.en)
    # 漏的是第一段里的一句：接在第一段的译文后面，不堆到第二段后面。
    assert missing in (first.zh or "")
    assert first.zh.endswith(f"<p>{missing}</p></li></ul>")
    assert _shown_zh(view).count("更广泛地说") == 1
    assert _shown_zh(view).count(missing) == 1


def _quote_copy(line_zh: str, first_zh: str, second_zh: str):
    """引文先写成一行，再分两段写一遍（没有链接）。整行那份收掉，两段套回引文里。"""
    q1 = (
        "Analysts expected the subscription business to slow down after the price change in the spring, but the "
        "numbers told a different story. Retention held steady across every region, and churn among new users fell "
        "for the third quarter in a row."
    )
    q2 = (
        "Management credited the bundle with most of that improvement, although it declined to break out the "
        "revenue. The open question is whether the bundle keeps working once the promotional pricing ends next year."
    )
    md = f"> {q1} {q2}\n\n{q1}\n\n{q2}\n\nAn unrelated closing paragraph talks about pricing and stays where it is.\n"
    source = str(render_body(md, {}))
    manifest = pr.source_manifest(source)
    target = splice_segments(manifest["source_snapshot"], {
        0: line_zh, 1: first_zh, 2: second_zh, 3: _CLOSING_ZH,
    })
    return article_pairs(source, target, provenance=pr.receipt(manifest, target))


_Q_LINE = (
    "<p>分析师原以为，春季调价之后订阅业务会放缓，但数字讲的是另一回事。各个地区的留存都保持稳定，"
    "新用户的流失率连续第三个季度下降。管理层把大部分改善归功于套餐，不过没有单独披露这部分收入。"
    "悬而未决的问题是，明年促销价结束以后，套餐还能不能继续奏效。</p>"
)
_Q1 = (
    "<p>分析师原以为，春季调价后订阅业务会放缓，但数字讲的是另一回事。各地区的留存都保持稳定，"
    "新用户的流失率连续第三个季度下降。</p>"
)
_Q2 = (
    "<p>管理层把大部分改善归功于套餐，但没有单独披露这部分收入。悬而未决的问题是，"
    "明年促销价结束后，套餐还能不能继续奏效。</p>"
)


def test_quote_line_copy_does_not_append_reworded_paragraphs():
    view = _quote_copy(_Q_LINE, _Q1, _Q2)
    assert view.correspondence == "mapped" and not view.notice
    shown = _shown_zh(view)
    assert shown.count("分析师原以为") == 1
    assert shown.count("悬而未决的问题") == 1
    assert [pair.zh for pair in view.pairs[:2]] == [f"<blockquote>{quoted}</blockquote>" for quoted in (_Q1, _Q2)]


def test_reworded_copy_with_a_new_number_or_name_is_still_shown():
    """字大半对得上，但带着别处没有的数字或英文名：可能是留下的那份漏了，照样挂。"""
    view = _quote_copy(_Q_LINE.replace("连续第三个季度", "连续3个季度"), _Q1, _Q2)
    assert "连续3个季度" in _shown_zh(view) and "连续第三个季度" in _shown_zh(view)
    # 挂在它那一段（第一段）的译文后面。
    assert "连续3个季度" in (view.pairs[0].zh or "")
    view = _quote_copy(_Q_LINE.replace("归功于套餐", "归功于Plus套餐"), _Q1, _Q2)
    assert "归功于Plus套餐" in _shown_zh(view)
    assert "归功于Plus套餐" in (view.pairs[1].zh or "")


def test_partial_novel_text_is_one_balanced_paragraph():
    """只挂一部分句子时，原来的列表、段落外壳不带过来，行内链接成对的留着。"""
    host = "<p>第一句已经显示过了。第三句也已经显示过了。</p>"
    # 以前新句排在最前面时，开头的 <ul><li><p> 跟着带过来，收尾的标签留在没挂的句子里，页面上缺了闭合。
    extra = (
        '<ul><li><p>第二句只在这一份里，提到<a href="https://example.invalid/new">新的资料</a>。'
        "第一句已经显示过了。</p></li><li><p>第三句也已经显示过了。</p></li></ul>"
    )
    assert _novel_zh(extra, host) == (
        '<p>第二句只在这一份里，提到<a href="https://example.invalid/new">新的资料</a>。</p>'
    )


def test_sentences_said_by_other_kept_pairs_count_as_shown():
    host = "<p>第一段的译文，讲演示和平台。</p>"
    context = "<p>第二段的译文，讲云部门和旧服务器产品线的关系。</p>"
    extra = "<p>第一段的译文，讲演示和平台。第二段的译文，讲云部门与旧服务器产品线的关系。</p>"
    assert _novel_zh(extra, host) != ""
    assert _novel_zh(extra, host, context) == ""
