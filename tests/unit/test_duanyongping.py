"""段永平博客夹具：公司问答留下，只讲方法的跳过，没日期的不进按天。"""

from pathlib import Path

from researchlib.author_voice import restrict
from researchlib.chunks import make_chunks
from researchlib.company_seed import SEEDS
from researchlib.ingest.duanyongping.cli import main
from researchlib.ingest.duanyongping.companies import extra_alias_keys, seed_alias_keys
from researchlib.ingest.duanyongping.emit import render_markdown, render_report, report_payload
from researchlib.ingest.duanyongping.extract import Block, lines_to_blocks, parse_text, parse_xml
from researchlib.ingest.duanyongping.segment import Post, classify, entries_from_blocks, segment_post
from researchlib.ingest.parse import parse_archive
from researchlib.mentions import alias_key

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "scripts" / "duanyongping" / "fixtures"


def _load(name: str):
    path = FIXTURES / name
    raw = path.read_text(encoding="utf-8")
    lines = parse_xml(raw) if name.endswith(".xml") else parse_text(raw)
    entries, mode, posts = entries_from_blocks(lines_to_blocks(lines))
    return entries, mode, posts


def _by_reason(entries, reason):
    return [entry for entry in entries if entry.reason == reason]


def test_extra_company_names_do_not_steal_seed_aliases():
    overlap = extra_alias_keys() & seed_alias_keys()
    assert overlap == set()
    # 种子里本来就有的，夹具里的公司名还能对上。
    assert any(alias_key("茅台") == alias_key(alias) for seed in SEEDS for alias in (seed.name_zh, *seed.aliases))


def test_fixture_pages_keep_company_talk_and_skip_method(capsys):
    entries, mode, posts = _load("sample.xml")
    assert mode == "posts"
    assert posts >= 8
    kept = [entry for entry in entries if entry.keep]
    skipped = [entry for entry in entries if not entry.keep]
    assert len(kept) == 5
    assert {tuple(entry.companies) for entry in kept} >= {("贵州茅台",), ("网易",), ("格力电器",)}
    apple = [entry for entry in kept if entry.companies == ["苹果"]]
    assert len(apple) == 2
    dated = [entry for entry in apple if entry.published]
    undated = [entry for entry in apple if not entry.published]
    assert len(dated) == 1 and len(undated) == 1
    assert undated[0].feed is False
    assert dated[0].published.startswith("2012-06-01")
    assert undated[0].kind == "qa"
    assert "iPhone" in undated[0].author
    assert "护城河是什么" not in undated[0].author

    maotai = next(entry for entry in kept if "贵州茅台" in entry.companies)
    assert maotai.published.startswith("2008-03-12")
    assert maotai.feed is True
    assert "贵不贵" in maotai.question
    assert "贵不贵" not in maotai.author
    assert "生意模式" in maotai.author

    netease = next(entry for entry in kept if entry.companies == ["网易"])
    assert "另一位网友" in netease.question
    assert "我也这么看" in netease.others
    assert "另一位" not in netease.author
    assert netease.published.startswith("2010-09-21")

    gree = next(entry for entry in kept if entry.companies == ["格力电器"])
    assert gree.published.startswith("2011-05-02 10:11:00")
    assert "格力" in gree.question
    assert "格力" not in gree.author
    assert "渠道" in gree.author

    assert any("能力圈" in entry.author for entry in _by_reason(skipped, "没有具体公司"))
    assert any("平常心" in (entry.author + entry.post_title) for entry in _by_reason(skipped, "没有具体公司"))
    assert any("比如茅台" in entry.author for entry in _by_reason(skipped, "公司只出现在举例里"))
    assert any(entry.post_title.startswith("【转载】") for entry in _by_reason(skipped, "没有段永平本人的话"))

    text_entries, text_mode, _posts = _load("sample.txt")
    assert text_mode == "posts"

    def signature(entry):
        return (
            entry.keep,
            entry.reason,
            tuple(entry.companies),
            entry.published,
            entry.question,
            entry.author,
            entry.others,
        )

    assert [signature(entry) for entry in text_entries] == [signature(entry) for entry in entries]

    payload = report_payload(entries, source="duanyongping-blog", origin="sample.xml", mode=mode, posts=posts)
    rendered = render_report(payload)
    assert "保留 5" in rendered
    assert "无日期" in rendered
    assert "公司只出现在举例里" in rendered
    print(rendered)
    assert "保留 5" in capsys.readouterr().out


def test_markdown_marks_undated_and_author_section(tmp_path: Path):
    entries, _mode, _posts = _load("sample.xml")
    undated = next(entry for entry in entries if entry.keep and not entry.published)
    dated = next(entry for entry in entries if entry.keep and entry.companies == ["格力电器"])
    (tmp_path / "undated.md").write_text(render_markdown(undated), encoding="utf-8")
    (tmp_path / "dated.md").write_text(render_markdown(dated), encoding="utf-8")
    docs, errors, files, _images = parse_archive(tmp_path, tmp_path / "work", id_mode="path")
    assert errors == []
    assert files == 2
    by_title = {doc.title: doc for doc in docs}
    missing = by_title[undated.title]
    assert missing.published_at is None
    assert missing.raw_meta["off_timeline"] is True
    assert missing.raw_meta["author_voice"] == "段永平"
    assert missing.external_id == undated.external_id
    # 文件名不带日期，导入器不会自己补一个发布时间。
    assert "2008" not in Path(missing.raw_meta["filename"]).name
    present = by_title[dated.title]
    assert present.published_at is not None
    assert present.published_at.year == 2011
    assert present.published_at.month == 5
    assert present.published_at.day == 2
    assert "off_timeline" not in present.raw_meta
    chunks = make_chunks(present.title, present.body_md)
    body, picked, restricted = restrict(present.title, chunks, present.raw_meta)
    assert restricted is True
    assert "格力" not in body
    assert "渠道" in body
    assert all(heading == "段永平" for heading, _text in picked)
    essay = next(entry for entry in entries if entry.keep and entry.published.startswith("2012-06-01"))
    essay_md = render_markdown(essay)
    assert "author_voice: \"all\"" in essay_md
    assert "## 段永平" not in essay_md


def test_arrow_reply_stays_one_entry_and_drops_the_label():
    post = Post(
        title="苹果",
        date="2010-08-10 03:00:00",
        repost=False,
        blocks=[
            Block("art753 (2010-08-10 03:35): 格力的渠道是不是护城河？", False, 12, 1),
            Block("段永平 → art753 (2010-08-10 03:46): 是。把渠道掌握在自己手里。", False, 12, 1),
        ],
        page=1,
    )
    kept = [entry for entry in (classify(item) for item in segment_post(post)) if entry.keep]
    assert len(kept) == 1
    assert kept[0].companies == ["格力电器"]
    assert kept[0].published == "2010-08-10 03:35:00"
    assert kept[0].author == "是。把渠道掌握在自己手里。"
    assert "art753" not in kept[0].author
    assert "格力" in kept[0].question


def test_cli_emit_only_prints_classification(tmp_path: Path, capsys):
    out = tmp_path / "out"
    code = main(
        [
            "--xml",
            str(FIXTURES / "sample.xml"),
            "--emit-only",
            "--out",
            str(out),
        ]
    )
    assert code == 0
    written = list(out.glob("dyp-*.md"))
    assert len(written) == 5
    assert (out / "report.json").is_file()
    printed = capsys.readouterr().out
    assert "保留 5" in printed
    assert "跳过 4" in printed
    assert "不进按天" in printed
