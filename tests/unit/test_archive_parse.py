import json
import zipfile
from pathlib import Path
from zoneinfo import ZoneInfo

from researchlib.ingest.archive import is_archive_note
from researchlib.ingest.parse import parse_archive, select_raw_json
from researchlib.ingest.wechat import main as wechat_main
from researchlib.ingest.zsxq import main as zsxq_main
from researchlib.sources import load_sources

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDAT\x08\xd7c\xf8\xff\xff?"
    b"\x00\x05\xfe\x02\xfe\xdc\xccY\xe7\x00\x00\x00\x00IEND\xaeB`\x82"
)


def test_whitelist_shape():
    sources = load_sources(Path(__file__).resolve().parents[2] / "config" / "sources.yaml")
    assert [source.id for source in sources if source.enabled] == [
        "fangwei-mp",
        "fangwei-zsxq",
        "chengfengpolang",
        "gudujinhuati",
        "linwenfeng-feishu",
        "stratechery",
        "heiheiwuchang",
    ]
    gudu = next(source for source in sources if source.id == "gudujinhuati")
    assert gudu.kind == "markdown"
    feishu = next(source for source in sources if source.id == "linwenfeng-feishu")
    assert feishu.kind == "feishu"
    assert feishu.timeline is False
    assert feishu.description == "飞书文档归档入库，保持启用。"
    timeline = {source.id: source.timeline for source in sources}
    assert timeline["fangwei-mp"] is True
    assert timeline["fangwei-zsxq"] is True
    assert timeline["chengfengpolang"] is True
    assert timeline["gudujinhuati"] is True
    assert timeline["linwenfeng-mp"] is True
    assert timeline["stratechery"] is True
    assert timeline["heiheiwuchang"] is True
    xueqiu = next(source for source in sources if source.id == "heiheiwuchang")
    assert xueqiu.name == "黑黑无常"
    assert xueqiu.author == "黑黑无常"
    assert xueqiu.kind == "markdown"
    assert xueqiu.enabled is True
    assert xueqiu.min_chars == 100
    assert "转发" in xueqiu.description and "评论" in xueqiu.description
    disabled = {source.id: source.kind for source in sources if not source.enabled}
    assert disabled == {
        "linwenfeng-mp": "wechat_mp",
    }
    strat = next(source for source in sources if source.id == "stratechery")
    assert strat.kind == "markdown"
    assert strat.enabled is True
    assert strat.timeline is True
    # 游客半截的 Daily、每周汇总几百词：不到 5500 字符标短帖。
    assert strat.min_chars == 5500


def test_missing_timeline_defaults_feishu_to_library(tmp_path: Path):
    path = tmp_path / "sources.yaml"
    path.write_text(
        """
sources:
  - id: vault
    name: 文库
    author: 甲
    kind: feishu
    enabled: true
  - id: feed
    name: 时间线
    author: 乙
    kind: markdown
    enabled: true
""",
        encoding="utf-8",
    )
    sources = {source.id: source for source in load_sources(path)}
    assert sources["vault"].timeline is False
    assert sources["feed"].timeline is True


def test_rejected_source_does_not_prepare(monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("拒绝的来源不应该连接数据库")

    monkeypatch.setattr("researchlib.ingest.cli.prepare", boom)
    assert wechat_main(["--source", "gudujinhuati", "--input", "/nowhere"]) == 2
    assert wechat_main(["--source", "linwenfeng-mp", "--input", "/nowhere"]) == 2
    assert wechat_main(["--source", "not-a-source", "--input", "/nowhere"]) == 2
    assert zsxq_main(["--source", "fangwei-mp", "--input", "/nowhere"]) == 2


def test_wechat_zips_and_zip_slip(tmp_path: Path):
    text_zip = tmp_path / "01-text.zip"
    image_zip = tmp_path / "02-images-a.zip"
    with zipfile.ZipFile(text_zip, "w") as archive:
        archive.writestr(
            "posts/2024-05-01_hello.md",
            """---
id: wx-1
title: 你好腾讯
date: 2024-05-01
---
正文提到腾讯。

![](images/pic.png)
""",
        )
        archive.writestr("../evil.md", "不应该被解压")
    with zipfile.ZipFile(image_zip, "w") as archive:
        archive.writestr("images/pic.png", PNG)

    docs, errors, files, images = parse_archive(tmp_path, tmp_path / "work", id_mode="path")
    assert files == 1
    assert images == 1
    assert errors == []
    assert docs[0].external_id == "wx-1"
    assert docs[0].title == "你好腾讯"
    assert docs[0].image_refs[0][1] is not None
    assert docs[0].image_refs[0][1].name == "pic.png"
    assert docs[0].published_at is not None
    assert docs[0].published_at.month == 5
    assert docs[0].raw_meta["date_source"] == "frontmatter"
    assert not any(path.name == "evil.md" for path in tmp_path.rglob("evil.md"))


def test_zsxq_uses_topic_json(tmp_path: Path):
    (tmp_path / "md").mkdir()
    (tmp_path / "images").mkdir()
    (tmp_path / "raw").mkdir()
    (tmp_path / "md" / "888888.md").write_text("星球正文里有茅台。\n\n![](../images/888888_1.png)\n", encoding="utf-8")
    (tmp_path / "images" / "888888_1.png").write_bytes(PNG)
    (tmp_path / "raw" / "888888.json").write_text(
        '{"topic_id": 888888, "create_time": "2024-02-03T04:05:06.000+0800", "title": "茅台短帖"}',
        encoding="utf-8",
    )
    docs, errors, _files, _images = parse_archive(tmp_path, tmp_path / "work", id_mode="zsxq")
    assert errors == []
    assert docs[0].external_id == "888888"
    assert docs[0].title == "茅台短帖"
    assert docs[0].published_at is not None
    assert docs[0].published_at.month == 2
    local = docs[0].published_at.astimezone(ZoneInfo("Asia/Shanghai"))
    assert (local.hour, local.minute) == (4, 5)
    assert docs[0].raw_meta["time_known"] is True
    assert docs[0].image_refs[0][1] is not None


def test_zsxq_date_only_frontmatter_does_not_zero_the_clock(tmp_path: Path):
    (tmp_path / "md").mkdir()
    (tmp_path / "raw").mkdir()
    (tmp_path / "md" / "888888.md").write_text(
        "---\ndate: 2024-10-19\n---\n星球正文。\n",
        encoding="utf-8",
    )
    (tmp_path / "raw" / "888888.json").write_text(
        '{"topic_id": 888888, "create_time": "2024-10-19T05:29:00+00:00", "talk": {"title": "下午的帖"}}',
        encoding="utf-8",
    )
    docs, errors, _files, _images = parse_archive(tmp_path, tmp_path / "work", id_mode="zsxq")
    assert errors == []
    local = docs[0].published_at.astimezone(ZoneInfo("Asia/Shanghai"))
    assert (local.year, local.month, local.day, local.hour, local.minute) == (2024, 10, 19, 13, 29)
    assert docs[0].raw_meta["time_known"] is True


def test_feishu_nested_ids(tmp_path: Path):
    note = tmp_path / "笔记"
    (note / "子文档").mkdir(parents=True)
    (note / "index.md").write_text("# 总览\n\n父文档。\n", encoding="utf-8")
    (note / "子文档" / "a.md").write_text("# 子篇\n\n子文档。\n", encoding="utf-8")
    # 文档树里 index.md 是「笔记」这一层自己的那一页，飞书导入不按说明文件筛。
    docs, errors, files, _images = parse_archive(tmp_path, tmp_path / "work", id_mode="path", skip_notes=False)
    assert errors == []
    assert files == 2
    by_id = {doc.external_id: doc for doc in docs}
    assert "笔记/index.md" in by_id
    assert by_id["笔记/子文档/a.md"].raw_meta["parent"] == "笔记/子文档"


def test_archive_notes_are_not_articles(tmp_path: Path):
    """归档自己的 README、INDEX 不是文章：方伟公众号归档根上的两份说明以前被当成文章导进去了。"""
    (tmp_path / "md" / "2026").mkdir(parents=True)
    (tmp_path / "raw" / "articles").mkdir(parents=True)
    (tmp_path / "README.md").write_text(
        "# 某公众号完整存档\n\n| 项目 | 值 |\n|---|---|\n| 已抓正文 | 2 |\n\n## 如何生成 / 更新\n",
        encoding="utf-8",
    )
    (tmp_path / "INDEX.md").write_text(
        "# 存档索引\n\n| 日期 | 标题 |\n| --- | --- |\n| 2026-10-17 | [腾讯](md/2026/2026-10-17-a.md) |\n",
        encoding="utf-8",
    )
    (tmp_path / "md" / "Index.markdown").write_text("目录\n", encoding="utf-8")
    (tmp_path / "md" / "2026" / "2026-10-17-a.md").write_text(
        "# 腾讯\n\n- 作者：方伟看十年\n- 时间：2026-10-17 07:00:00 (UTC+8)\n\n正文提到腾讯。\n",
        encoding="utf-8",
    )
    # 文件名里带 readme、index 的普通文章照收：只认整个名字。
    (tmp_path / "md" / "2026" / "2026-10-18-readme-index.md").write_text(
        "# 读书笔记\n\n- 时间：2026-10-18 07:00:00 (UTC+8)\n\n正文。\n",
        encoding="utf-8",
    )
    docs, errors, files, _images = parse_archive(tmp_path, tmp_path / "work", id_mode="path")
    assert errors == []
    assert files == 2
    # 外部 id 照旧是相对归档根的路径，不因为根上少了两个文件就变。
    assert sorted(doc.external_id for doc in docs) == [
        "md/2026/2026-10-17-a.md",
        "md/2026/2026-10-18-readme-index.md",
    ]
    # 星球导出 md/ 里的 INDEX.md、README.md 也一样。
    zsxq = tmp_path / "zsxq"
    (zsxq / "md").mkdir(parents=True)
    (zsxq / "md" / "888888.md").write_text("- 时间：2024-11-28 10:40:22\n\n正文。\n", encoding="utf-8")
    (zsxq / "md" / "INDEX.md").write_text("# 话题索引（按时间倒序）\n\n- [正文](888888.md)\n", encoding="utf-8")
    (zsxq / "md" / "readme.md").write_text("导出说明\n", encoding="utf-8")
    docs, errors, files, _images = parse_archive(zsxq, tmp_path / "work-zsxq", id_mode="zsxq")
    assert errors == []
    assert files == 1
    assert [doc.external_id for doc in docs] == ["888888"]


def test_archive_note_names():
    for name in ("README.md", "readme.markdown", "INDEX.md", "md/INDEX.md", "笔记\\index.md"):
        assert is_archive_note(name), name
    for name in ("README.txt", "index.html", "2026-10-18-readme.md", "_index.md", "主页-Jason 林文丰.md", "INDEX"):
        assert not is_archive_note(name), name


def _shanghai(value):
    return value.astimezone(ZoneInfo("Asia/Shanghai"))


def test_wechat_chinese_publish_time_and_filename(tmp_path: Path):
    (tmp_path / "dated.md").write_text(
        "---\ntitle: 中文日期\npublish_time: 2024年5月1日 21:30\n---\n正文\n",
        encoding="utf-8",
    )
    (tmp_path / "笔记_20240707_补充.md").write_text("没有前言。\n", encoding="utf-8")
    (tmp_path / "markup.md").write_text(
        '<em id="publish_time">2022年3月4日 08:09</em>\n\n正文\n',
        encoding="utf-8",
    )
    docs, errors, files, _images = parse_archive(tmp_path, tmp_path / "work", id_mode="path")
    assert errors == []
    assert files == 3
    by_name = {doc.raw_meta["filename"]: doc for doc in docs}
    chinese = _shanghai(by_name["dated.md"].published_at)
    assert (chinese.year, chinese.month, chinese.day, chinese.hour, chinese.minute) == (2024, 5, 1, 21, 30)
    assert by_name["dated.md"].raw_meta["date_source"] == "frontmatter"
    named = _shanghai(by_name["笔记_20240707_补充.md"].published_at)
    assert (named.year, named.month, named.day) == (2024, 7, 7)
    assert by_name["笔记_20240707_补充.md"].raw_meta["date_source"] == "filename"
    marked = _shanghai(by_name["markup.md"].published_at)
    assert (marked.year, marked.month, marked.day, marked.hour, marked.minute) == (2022, 3, 4, 8, 9)
    assert by_name["markup.md"].raw_meta["date_source"] == "markup"


def test_zsxq_nested_topic_uses_shanghai_day(tmp_path: Path):
    (tmp_path / "md").mkdir()
    (tmp_path / "raw").mkdir()
    (tmp_path / "md" / "999.md").write_text("嵌套正文。\n", encoding="utf-8")
    (tmp_path / "raw" / "999.json").write_text(
        json.dumps(
            {
                "succeeded": True,
                "resp_data": {
                    "topic": {
                        "topic_id": 999,
                        "create_time": "2023-12-31T16:05:00.000+0000",
                        "talk": {"text": "嵌套"},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    page = {
        "resp_data": {
            "topics": [
                {"topic_id": 111, "create_time": "2020-01-02T03:04:05+08:00", "talk": {"title": "甲篇"}},
                {"topic_id": 222, "create_time": "2020-01-03T00:00:00+08:00", "title": "乙篇"},
            ]
        }
    }
    (tmp_path / "raw" / "page.json").write_text(json.dumps(page), encoding="utf-8")
    (tmp_path / "md" / "111.md").write_text("甲正文\n", encoding="utf-8")
    (tmp_path / "md" / "222.md").write_text("乙正文\n", encoding="utf-8")
    docs, errors, _files, _images = parse_archive(tmp_path, tmp_path / "work", id_mode="zsxq")
    assert errors == []
    by_id = {doc.external_id: doc for doc in docs}
    nested = _shanghai(by_id["999"].published_at)
    assert (nested.year, nested.month, nested.day, nested.hour, nested.minute) == (2024, 1, 1, 0, 5)
    assert by_id["999"].raw_meta["date_source"] == "meta"
    assert by_id["111"].title == "甲篇"
    assert _shanghai(by_id["111"].published_at).day == 2
    assert by_id["222"].title == "乙篇"
    assert _shanghai(by_id["222"].published_at).day == 3


def test_select_raw_json_keeps_topics_when_raw_root_files_exist(tmp_path: Path):
    raw = tmp_path / "raw"
    (raw / "topics").mkdir(parents=True)
    group = raw / "group.json"
    state = raw / "state.json"
    topic = raw / "topics" / "888888.json"
    for path in (group, state, topic):
        path.write_text("{}", encoding="utf-8")
    chosen = select_raw_json([group, state, topic])
    assert topic in chosen and group in chosen and state in chosen
    assert chosen[0] == topic


def test_zsxq_topics_json_wins_over_raw_root_files(tmp_path: Path):
    """raw/group.json、state.json 让「父目录叫 raw」的列表非空，不能因此跳过 raw/topics。"""
    (tmp_path / "md").mkdir()
    (tmp_path / "raw" / "topics").mkdir(parents=True)
    (tmp_path / "md" / "2024-11-28-888888.md").write_text("正文。\n", encoding="utf-8")
    (tmp_path / "md" / "INDEX.md").write_text("- 时间：2020-01-01 01:02:03\n目录\n", encoding="utf-8")
    (tmp_path / "raw" / "group.json").write_text(
        json.dumps({"topics": [{"topic_id": 888888, "create_time": "2020-01-01T00:00:00.000+0800"}]}),
        encoding="utf-8",
    )
    (tmp_path / "raw" / "state.json").write_text('{"cursor": "x"}', encoding="utf-8")
    (tmp_path / "raw" / "topics" / "888888.json").write_text(
        '{"topic_id": 888888, "create_time": "2024-11-28T10:40:22.487+0800", "title": "真实钟点"}',
        encoding="utf-8",
    )
    docs, errors, _files, _images = parse_archive(tmp_path, tmp_path / "work", id_mode="zsxq")
    assert errors == []
    assert [doc.external_id for doc in docs] == ["888888"]
    assert is_archive_note("INDEX.md")
    local = _shanghai(docs[0].published_at)
    assert (local.year, local.month, local.day, local.hour, local.minute, local.second) == (2024, 11, 28, 10, 40, 22)
    assert docs[0].raw_meta["date_source"] == "meta"
    assert docs[0].raw_meta["time_known"] is True
    assert docs[0].raw_meta["create_time"] == "2024-11-28T10:40:22.487+0800"
    assert docs[0].title == "真实钟点"


def test_zsxq_body_time_when_json_has_no_clock(tmp_path: Path):
    (tmp_path / "md").mkdir()
    (tmp_path / "raw").mkdir()
    (tmp_path / "md" / "2024-11-28-888888.md").write_text(
        "- 时间：2024-11-28 10:40:22\n\n正文。\n",
        encoding="utf-8",
    )
    (tmp_path / "raw" / "group.json").write_text("{}", encoding="utf-8")
    (tmp_path / "raw" / "state.json").write_text("{}", encoding="utf-8")
    docs, errors, _files, _images = parse_archive(tmp_path, tmp_path / "work", id_mode="zsxq")
    assert errors == []
    local = _shanghai(docs[0].published_at)
    assert (local.hour, local.minute, local.second) == (10, 40, 22)
    assert docs[0].raw_meta["date_source"] == "body"
    assert docs[0].raw_meta["time_known"] is True


def test_path_year_rejects_crawl_stamped_frontmatter(tmp_path: Path):
    """Stratechery-style YYYY/slug: distrust frontmatter dated in another year."""
    year_dir = tmp_path / "2025"
    year_dir.mkdir()
    (year_dir / "autonomous-cars-and-deep-research.md").write_text(
        """---
id: "2025/autonomous-cars-and-deep-research"
title: Autonomous Cars and Deep Research
published: "2026-10-21"
date: "2026-10-21"
---
Body about autonomous cars.
""",
        encoding="utf-8",
    )
    # Matching year still wins.
    (year_dir / "ok-same-year.md").write_text(
        """---
id: "2025/ok-same-year"
title: Same Year
published: "2025-06-01"
date: "2025-06-01"
---
Fine.
""",
        encoding="utf-8",
    )
    docs, errors, files, _images = parse_archive(tmp_path, tmp_path / "work", id_mode="path")
    assert errors == []
    assert files == 2
    by_id = {doc.external_id: doc for doc in docs}
    bad = by_id["2025/autonomous-cars-and-deep-research"]
    assert bad.published_at is None
    assert bad.raw_meta.get("path_year_mismatch") is True
    assert bad.raw_meta.get("path_year") == 2025
    assert bad.raw_meta.get("date_source") in (None, "")
    good = by_id["2025/ok-same-year"]
    assert good.published_at is not None
    assert good.published_at.astimezone(ZoneInfo("Asia/Shanghai")).year == 2025
    assert good.raw_meta.get("path_year_mismatch") is not True


def test_wechat_literal_backslash_n_becomes_paragraphs(tmp_path: Path):
    """抓取脚本把换行写成字面上的反斜杠 n 时，整篇挤成一行：挨着中文的换回换行，代码里的不动。"""
    body = (
        "指数兜兜转转两年，又回到了 3600 多点。\\n\\n熊市里股价跌八成也不等于便宜。\\n\\n"
        "比如美国运通 Platinum\\n\\nAMD 也一样。\\r\\n能做的就是耐心等。\\n"
    )
    (tmp_path / "2026-10-23-text.md").write_text(
        "# 如何缓解熊市中的精神内耗？\n\n- 作者：方伟看十年\n- 时间：2026-10-23 09:22:39 (UTC+8)\n\n"
        + body
        + '\n\n```c\nprintf("%d\\n", total);\n```\n',
        encoding="utf-8",
    )
    docs, errors, _files, _images = parse_archive(tmp_path, tmp_path / "work", id_mode="path")
    assert errors == []
    text = docs[0].body_md
    assert "又回到了 3600 多点。\n\n熊市里股价跌八成也不等于便宜。\n\n比如" in text
    # 同一行里挨着英文的那处也一起换掉；单个换行留着（页面按 nl2br 显示成换行）。
    assert "Platinum\n\nAMD 也一样。\n能做的就是耐心等。" in text
    assert 'printf("%d\\n", total);' in text
    assert text.count("\\n") == 1


def test_body_time_reads_the_written_time_zone():
    from researchlib.textutil import _body_time_raw, parse_datetime

    def when(line):
        raw = _body_time_raw(f"# 标题\n\n- 时间：{line}\n\n正文")
        return parse_datetime(raw) if raw else None

    shanghai = when("2026-09-27 14:41 (UTC+8)")
    assert shanghai.isoformat() == "2026-09-27T14:41:00+08:00"
    assert when("2026-09-27 14:41 UTC+8") == shanghai
    assert when("2026-09-27 14:41（北京时间）") == shanghai
    assert when("2026-09-27 14:41") == shanghai
    assert when("2026-09-27 06:41 (UTC)") == shanghai
    assert when("2026-09-27 01:41 GMT-5") == shanghai
    # 认不出的时区：只留日期，当没有钟点，不按上海时间读错。
    unknown = _body_time_raw("# 标题\n\n- 时间：2026-09-27 14:41 (PST)\n")
    assert unknown == "2026-09-27"
