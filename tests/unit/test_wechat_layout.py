"""老公众号文章的图按原文页的位置摆：字一个不改，只挪单独成行的图。不连网。"""

from researchlib.revisions import images_moved_only
from researchlib.wechat_html import page_markdown
from researchlib.wechat_layout import image_key, original_link, place_like_page

SN = "a" * 32
HEADER = (
    "# 茅台直销\n\n- 作者：方伟\n- 时间：2024-05-01 08:00:00 (UTC+8)\n"
    f"- sn：`{SN}`\n- 原链接：https://mp.weixin.qq.com/s?sn={SN}\n\n"
)
PARAS = [
    "茅台今年的批价一直在两千块上下，渠道库存不算高。",
    "我们先看一下过去五年的收入结构，直销占比提升很快。",
    "下面这张图是i茅台上线以后每个季度的直销收入。",
    "可以看到直销收入的增速明显快于整体收入增速。",
    "再看毛利率，直销渠道的毛利率要高出十个点以上。",
    "所以结论是：直销占比提升会继续推高整体毛利率。",
]


def _img(number: int, fmt: str = "png") -> str:
    return f"https://mmbiz.qpic.cn/mmbiz_{fmt}/ID{number}xyz/640?wx_fmt={fmt}"


def _page(*parts: str) -> str:
    """parts：字是一段，数字是第几张图。转成原文页 #js_content 再转回 Markdown，和抓取时一样。"""
    html = "".join(
        f'<p><img data-src="{_img(int(part))}&amp;from=appmsg"></p>' if part.isdigit() else f"<p>{part}</p>"
        for part in parts
    )
    return page_markdown(f'<div id="js_content">{html}</div>')[0]


# 原文页：第 1 张图在第三段后面，第 2 张在第五段后面，第 3 张在最后。
PAGE = _page(PARAS[0], PARAS[1], PARAS[2], "1", PARAS[3], PARAS[4], "2", PARAS[5], "3")
# 极致了估的位置：第 N 张图放在第 N 段后面。
ESTIMATED = (
    HEADER
    + f"{PARAS[0]}\n\n![]({_img(1)})\n\n{PARAS[1]}\n\n![]({_img(2)})\n\n{PARAS[2]}\n\n![]({_img(3)})\n\n"
    + f"{PARAS[3]}\n\n{PARAS[4]}\n\n{PARAS[5]}\n"
)


def _order(body: str) -> list[str]:
    """正文里有字的行和图，按出现顺序，图写成 img1、img2……"""
    order = []
    for line in body.split("\n"):
        if line.startswith("![]("):
            order.append("img" + line.split("/ID")[1].split("xyz")[0])
        elif line in PARAS:
            order.append(f"p{PARAS.index(line)}")
    return order


def test_images_go_back_where_the_original_page_has_them():
    placed = place_like_page(ESTIMATED, PAGE)
    assert placed.reason == "" and placed.images == 3 and placed.moved == 3
    assert _order(placed.body_md) == ["p0", "p1", "p2", "img1", "p3", "p4", "img2", "p5", "img3"]
    # 归档头原样留在最前面，字一个没改，图还是归档里那几行（地址不换成原文页的）。
    assert placed.body_md.startswith(HEADER)
    assert images_moved_only(ESTIMATED, placed.body_md)
    assert f"![]({_img(1)})" in placed.body_md and "from=appmsg" not in placed.body_md
    assert placed.body_md.endswith("\n")


def test_local_images_pair_by_order_when_the_counts_match():
    local = ESTIMATED
    for number in (1, 2, 3):
        local = local.replace(_img(number), f"images/{number}.png")
    placed = place_like_page(local, PAGE)
    assert placed.reason == "" and placed.moved == 3
    lines = [line for line in placed.body_md.split("\n") if line.strip()]
    assert lines.index("![](images/1.png)") == lines.index(PARAS[2]) + 1
    assert lines.index("![](images/2.png)") == lines.index(PARAS[4]) + 1
    assert lines[-1] == "![](images/3.png)"


def test_local_images_stay_when_the_page_has_a_different_number_of_images():
    local = ESTIMATED
    for number in (1, 2, 3):
        local = local.replace(_img(number), f"images/{number}.png")
    page = _page(PARAS[0], "9", PARAS[1], PARAS[2], "1", PARAS[3], PARAS[4], "2", PARAS[5], "3")
    placed = place_like_page(local, page)
    assert placed.reason == "图的张数对不上（归档 3 张，原文页 4 张）"
    assert placed.body_md == local


def test_image_ids_pair_even_when_the_page_has_extra_images():
    page = _page(PARAS[0], "9", PARAS[1], PARAS[2], "1", PARAS[3], PARAS[4], "2", PARAS[5], "3")
    placed = place_like_page(ESTIMATED, page)
    assert placed.reason == ""
    assert _order(placed.body_md) == ["p0", "p1", "p2", "img1", "p3", "p4", "img2", "p5", "img3"]


def test_images_in_another_order_than_the_page_stay():
    page = _page(PARAS[0], PARAS[1], PARAS[2], "2", PARAS[3], PARAS[4], "1", PARAS[5], "3")
    placed = place_like_page(ESTIMATED, page)
    assert placed.reason == "图的先后和原文页不一样" and placed.body_md == ESTIMATED


def test_text_that_does_not_match_the_page_stays():
    other = [
        "这是另一篇文章，讲的是腾讯的广告业务，今年视频号的广告收入涨得很快。",
        "视频号的广告加载率还很低，朋友圈的广告价格今年也没有涨，小程序的抽成也没变。",
        "所以广告收入的增长主要靠视频号，明年还能再看到三成以上的增速。",
        "游戏和金融科技两块今年都比较平，云业务的利润率在慢慢往上走。",
    ]
    page = _page(other[0], "1", other[1], "2", other[2], other[3], "3")
    placed = place_like_page(ESTIMATED, page)
    assert placed.reason.startswith("字只有") and placed.reason.endswith("对得上原文页")
    assert placed.body_md == ESTIMATED


def test_image_on_the_same_line_as_text_stays():
    body = HEADER + f"{PARAS[0]}![]({_img(1)})\n\n{PARAS[1]}\n\n{PARAS[2]}\n"
    placed = place_like_page(body, _page(PARAS[0], PARAS[1], PARAS[2], "1"))
    assert placed.reason == "有图和字在同一行" and placed.body_md == body


def test_images_already_in_place_move_nothing():
    body = HEADER + f"{PARAS[0]}\n\n{PARAS[1]}\n\n{PARAS[2]}\n\n![]({_img(1)})\n\n{PARAS[3]}\n"
    placed = place_like_page(body, _page(PARAS[0], PARAS[1], PARAS[2], "1", PARAS[3]))
    assert placed.reason == "" and placed.moved == 0 and placed.body_md == body


def test_image_at_the_top_of_the_page_stays_below_the_archive_header():
    body = HEADER + f"{PARAS[0]}\n\n{PARAS[1]}\n\n![]({_img(1)})\n\n{PARAS[2]}\n"
    placed = place_like_page(body, _page("1", PARAS[0], PARAS[1], PARAS[2]))
    assert placed.reason == "" and placed.moved == 1
    assert placed.body_md.startswith(HEADER + f"![]({_img(1)})\n\n{PARAS[0]}")


def test_page_paragraphs_joined_in_the_archive_put_the_image_at_the_nearest_line_break():
    # 归档把原文页的两段并成了一行：图在两段中间，行里的字不拆，图放到离它最近的那个换行处。
    longer = PARAS[2] + "这一段再多写几句，让图离这一行的结尾更远一些，离开头更近。"
    joined = PARAS[1] + longer
    body = HEADER + f"{PARAS[0]}\n\n{joined}\n\n{PARAS[3]}\n\n![]({_img(1)})\n"
    page = _page(PARAS[0], PARAS[1], "1", longer, PARAS[3])
    placed = place_like_page(body, page)
    assert placed.reason == "" and placed.moved == 1
    lines = [line for line in placed.body_md.split("\n") if line.strip()]
    assert lines.index(f"![]({_img(1)})") == lines.index(joined) - 1


def test_formatting_differences_do_not_count_against_the_match():
    # 归档是别的工具转的 Markdown：加粗、链接、全角数字、标点和原文页不一样，只比字母、数字和汉字。
    body = (
        HEADER
        + "**茅台**今年的批价一直在两千块上下 渠道库存不算高\n\n![](images/1.png)\n\n"
        + "我们先看一下过去５年的[收入结构](https://example.com/a)，直销占比提升很快！\n\n"
        + "下面这张图是i茅台上线以后每个季度的直销收入：\n"
    )
    placed = place_like_page(body, _page(PARAS[0], PARAS[1].replace("五", "5"), PARAS[2], "1"))
    assert placed.reason == "" and placed.moved == 1
    assert placed.body_md.rstrip("\n").endswith("每个季度的直销收入：\n\n![](images/1.png)")


def test_moved_only_means_same_words_and_same_images_in_the_same_order():
    placed = place_like_page(ESTIMATED, PAGE).body_md
    assert images_moved_only(ESTIMATED, placed)
    assert not images_moved_only(ESTIMATED, ESTIMATED)
    assert not images_moved_only(ESTIMATED, placed.replace(PARAS[3], PARAS[3] + "补一句。"))
    assert not images_moved_only(ESTIMATED, placed.replace(f"![]({_img(2)})\n", ""))
    swapped = placed.replace(_img(1), "SWAP").replace(_img(2), _img(1)).replace("SWAP", _img(2))
    assert not images_moved_only(ESTIMATED, swapped)


def test_image_key_is_the_id_in_the_image_host_path():
    assert image_key("https://mmbiz.qpic.cn/mmbiz_png/AbC123/640?wx_fmt=png&from=appmsg") == "AbC123"
    assert image_key("http://mmbiz.qpic.cn/sz_mmbiz_jpg/AbC123/0?wx_fmt=jpeg") == "AbC123"
    assert image_key("images/1.png") is None
    assert image_key("https://example.com/a/b.png?x=1") == "example.com/a/b.png"


def test_original_link_reads_the_archive_header():
    long_form = "http://mp.weixin.qq.com/s?__biz=MzI1&amp;mid=2&amp;idx=1&amp;sn=" + SN + "&amp;chksm=x#rd"
    body = f"# 标题\n\n- 作者：方伟\n- 原链接：{long_form}\n\n正文。\n"
    assert original_link(body) == (
        "https://mp.weixin.qq.com/s?__biz=MzI1&mid=2&idx=1&sn=" + SN + "&chksm=x"
    )
    assert original_link("# 标题\n\n正文。\n", {"url": "https://mp.weixin.qq.com/s/AbCd"}) == (
        "https://mp.weixin.qq.com/s/AbCd"
    )
    assert original_link("# 标题\n\n- 原链接：https://example.com/a\n\n正文。\n") == ""


def test_frontmatter_stays_on_top():
    body = f"---\ntitle: 茅台直销\n---\n\n{PARAS[0]}\n\n{PARAS[1]}\n\n![]({_img(1)})\n\n{PARAS[2]}\n"
    placed = place_like_page(body, _page("1", PARAS[0], PARAS[1], PARAS[2]))
    assert placed.reason == "" and placed.moved == 1
    assert placed.body_md.startswith(f"---\ntitle: 茅台直销\n---\n\n![]({_img(1)})\n\n{PARAS[0]}")


def test_page_with_much_less_text_is_not_aligned():
    placed = place_like_page(ESTIMATED, _page(PARAS[0], "1", "2", "3"))
    assert placed.reason == "原文页的字比归档里少一成以上" and placed.body_md == ESTIMATED


def test_section_heading_after_the_archive_header_takes_part_in_the_alignment():
    # 原文页上头图在小标题前面：小标题是正文，图挪到它前面，不挪到归档头前面。
    body = HEADER + f"## 一、直销\n\n{PARAS[0]}\n\n![]({_img(1)})\n\n{PARAS[1]}\n"
    page = _page("1", "一、直销", PARAS[0], PARAS[1]).replace("\n\n一、直销", "\n\n## 一、直销")
    placed = place_like_page(body, page)
    assert placed.reason == "" and placed.moved == 1
    assert placed.body_md.startswith(HEADER + f"![]({_img(1)})\n\n## 一、直销")
