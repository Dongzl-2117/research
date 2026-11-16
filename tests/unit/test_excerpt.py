"""超长文节选页的切节、定位、截取（检索按片段直达，B）。"""

from researchlib.web.excerpt import build_outline, locate, path, split_sections, window


def _doc(sections: int = 30, size: int = 3) -> str:
    parts = ["开头一段，说明这份资料怎么来的。"]
    for index in range(sections):
        level = "##" if index % 5 == 0 else "###"
        parts.append(f"{level} 第 {index} 节")
        for line in range(size):
            parts.append(f"第 {index} 节第 {line} 段：王小明讲渠道费用和会员费，" + "经营数据见年报附注。" * 6)
    return "\n\n".join(parts)


def test_sections_cover_the_whole_text_and_skip_fences():
    md = _doc() + "\n\n```\n# 这不是标题\n```\n\n## 最后一节\n\n结尾。"
    sections = split_sections(md)
    assert "".join(md[s.start:s.end] for s in sections) == md
    assert all("这不是标题" != s.title for s in sections)
    assert sections[0].level == 0 and sections[-1].title == "最后一节"


def test_path_follows_heading_levels():
    outline = build_outline(_doc())
    index = next(i for i, s in enumerate(outline.sections) if s.title == "第 7 节")
    assert [title for _i, title in path(outline, index)] == ["第 5 节", "第 7 节"]


def test_locate_finds_the_section_of_a_chunk():
    md = _doc()
    outline = build_outline(md)
    chunk = "第 12 节第 1 段：王小明讲渠道费用和会员费，" + "经营数据见年报附注。" * 6
    index = locate(outline, chunk, "第 12 节")
    assert outline.sections[index].title == "第 12 节"
    assert locate(outline, "页面上没有的一段话，这里一个字都对不上。") is None


def test_window_pads_short_sections_and_trims_huge_ones():
    md = "## 短\n\n一句话。\n\n## 次\n\n" + "第二节的内容。" * 300 + "\n\n## 再次\n\n第三节。"
    outline = build_outline(md)
    view = window(md, outline, 0, 0)
    assert view.first == 0 and view.last == 1 and "第二节的内容" in view.md
    long = "## 很长的一节\n\n" + "\n\n".join(f"第 {k} 段。" + "填充的文字很多。" * 40 for k in range(200))
    outline = build_outline(long)
    target = "第 120 段。" + "填充的文字很多。" * 40
    view = window(long, outline, 0, 0, target)
    assert view.trimmed and "第 120 段" in view.md and "第 5 段。" not in view.md
    assert view.md.startswith("## 很长的一节") and len(view.md) < 12000


def test_heading_with_long_whitespace_is_linear():
    import time

    md = "# x" + " " * 100000 + "y\n\n正文。\n\n## 第一章 话题 ##\n\n### C#\n\n# 标题   \n"
    started = time.perf_counter()
    sections = split_sections(md)
    assert time.perf_counter() - started < 0.5
    assert [s.title for s in sections][1:] == ["第一章 话题", "C", "标题"]
