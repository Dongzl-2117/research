import json
import zipfile
from pathlib import Path
from zoneinfo import ZoneInfo

from researchlib.ingest.parse import parse_archive
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
    ]
    gudu = next(source for source in sources if source.id == "gudujinhuati")
    assert gudu.kind == "markdown"
    feishu = next(source for source in sources if source.id == "linwenfeng-feishu")
    assert feishu.kind == "feishu"
    assert feishu.description == "飞书文档归档入库，保持启用。"
    disabled = {source.id: source.kind for source in sources if not source.enabled}
    assert disabled == {
        "linwenfeng-mp": "wechat_mp",
    }


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
    assert docs[0].image_refs[0][1] is not None


def test_feishu_nested_ids(tmp_path: Path):
    note = tmp_path / "笔记"
    (note / "子文档").mkdir(parents=True)
    (note / "index.md").write_text("# 总览\n\n父文档。\n", encoding="utf-8")
    (note / "子文档" / "a.md").write_text("# 子篇\n\n子文档。\n", encoding="utf-8")
    docs, errors, files, _images = parse_archive(tmp_path, tmp_path / "work", id_mode="path")
    assert errors == []
    assert files == 2
    by_id = {doc.external_id: doc for doc in docs}
    assert "笔记/index.md" in by_id
    assert by_id["笔记/子文档/a.md"].raw_meta["parent"] == "笔记/子文档"


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
