"""回填之后的模型费用估算：只算字数和仓库里的单价，不连网、不调模型。"""

import inspect
import json
from pathlib import Path

from researchlib import llm_cost
from researchlib.excerpt import DIGEST_LIMIT, VIEW_WINDOW
from researchlib.llm_cost import (
    ASSUMED_BODY_CHARS,
    HEADER_CHARS,
    OUTPUT_TOKENS_HIGH,
    OUTPUT_TOKENS_LOW,
    ArticleSpec,
    build_estimate,
    dollars,
    render_estimate,
    specs_from_listing,
)


def test_estimator_does_not_call_the_model_or_the_network():
    source = inspect.getsource(llm_cost)
    assert "urlopen" not in source
    assert "llm.chat" not in source
    assert ".chat(" not in source


def test_digest_cost_uses_documented_prices_and_a_planning_band():
    prompt = 100
    specs = [ArticleSpec(chars=1000, assumed=False, need_digest=True, deep_mentions=0, images=0)]
    estimate = build_estimate(specs, digest_prompt_chars=prompt, views_prompt_chars=20)
    assert estimate.digest.calls == 1
    assert estimate.digest.input_tokens == 1000 + prompt + HEADER_CHARS
    assert estimate.digest.output_low == OUTPUT_TOKENS_LOW
    assert estimate.digest.output_high == OUTPUT_TOKENS_HIGH
    low = dollars(estimate.digest.input_tokens, estimate.digest.output_low)
    high = dollars(estimate.digest.input_tokens, estimate.digest.output_high)
    assert low == (estimate.digest.input_tokens / 1_000_000) * 0.15 + (OUTPUT_TOKENS_LOW / 1_000_000) * 0.60
    assert dollars(estimate.digest.input_tokens, estimate.digest.output_low, peak=True) == low * 2
    text = render_estimate(estimate, provider_text="当前提供方: DeepSeek（deepseek-flash，reasoning_effort=max）")
    assert f"{low:.2f}" in text and f"{high:.2f}" in text
    assert "非高峰" in text and "高峰" in text
    assert "每晚最多 80" in text and "research-process-new" in text
    assert "不调模型" in text
    assert estimate.views is not None and estimate.views.calls == 0
    assert estimate.views_are_upper is False


def test_long_article_views_are_split_by_the_window():
    chars = VIEW_WINDOW + 100
    specs = [ArticleSpec(chars=chars, deep_mentions=2, images=3, need_digest=True)]
    estimate = build_estimate(specs, digest_prompt_chars=10, views_prompt_chars=50)
    assert estimate.views is not None
    assert estimate.views.calls == 2
    assert estimate.views.input_tokens == chars + 2 * 50
    assert estimate.digest.input_tokens == min(chars, DIGEST_LIMIT) + 10 + HEADER_CHARS
    assert estimate.images == 3
    text = render_estimate(estimate, provider_text="当前提供方: Grok OAuth")
    assert "订阅" in text or "Grok" in text
    assert "识图: 3 张" in text
    assert "NIGHTLY_OCR=0" in text


def test_listing_without_bodies_uses_the_assumed_length_and_skips_non_articles(tmp_path):
    path = tmp_path / "post-history.jsonl"
    rows = [
        {"sn": "a" * 32, "title": "一篇", "skip": ""},
        {"sn": "b" * 32, "title": "视频", "skip": "纯视频或纯音乐"},
        {"sn": "c" * 32, "title": "又一篇", "skip": ""},
    ]
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
    specs, skipped = specs_from_listing(path)
    assert skipped == 1 and len(specs) == 2
    assert all(spec.assumed and spec.chars == ASSUMED_BODY_CHARS and spec.deep_mentions is None for spec in specs)
    estimate = build_estimate(specs, digest_prompt_chars=10, views_prompt_chars=10, skipped=skipped)
    assert estimate.assumed == 2 and estimate.digest.calls == 2
    assert estimate.views_are_upper and estimate.views is not None and estimate.views.articles == 2
    text = render_estimate(estimate, provider_text="当前提供方: DeepSeek")
    assert "假设" in text and "上沿" in text and "不要和摘要加在一起" in text


def test_listing_uses_archive_lengths_when_the_body_is_on_disk(tmp_path):
    sn = "d" * 32
    listing = tmp_path / "post-history.jsonl"
    listing.write_text(json.dumps({"sn": sn, "title": "有正文", "skip": ""}) + "\n", encoding="utf-8")
    folder = tmp_path / "inbox" / "md" / "2024"
    folder.mkdir(parents=True)
    folder.joinpath(f"2024-01-02-{sn}.md").write_text("头\n\n" + "正" * 100, encoding="utf-8")
    chars = llm_cost.archive_chars(tmp_path / "inbox")
    specs, skipped = specs_from_listing(listing, chars_by_sn=chars)
    assert skipped == 0 and specs[0].assumed is False and specs[0].chars == 100 + len("头\n\n")


def test_short_text_is_not_digested_and_schedule_caps_match_the_scripts():
    specs = [ArticleSpec(chars=10, assumed=False, need_digest=True, deep_mentions=0, images=0)]
    estimate = build_estimate(specs, digest_prompt_chars=10, views_prompt_chars=10)
    assert estimate.digest.calls == 0 and estimate.short_skipped == 1
    root = Path(__file__).resolve().parents[2]
    nightly = (root / "scripts" / "nightly.sh").read_text(encoding="utf-8")
    late = (root / "scripts" / "crawl-latepost.sh").read_text(encoding="utf-8")
    assert f"DIGEST_LIMIT:-{llm_cost.NIGHTLY_DIGEST_LIMIT}" in nightly
    assert f"VIEWS_LIMIT:-{llm_cost.NIGHTLY_VIEWS_LIMIT}" in nightly
    assert 'THESES_ENABLED="${THESES_ENABLED:-0}"' in nightly
    assert 'NIGHTLY_OCR="${NIGHTLY_OCR:-0}"' in nightly
    # 晚点的定时抓取只当场处理这次抓进来的（按本次导入回执），不按来源补积压。
    assert "crawl_with_receipt research-crawl-wechat" in late and 'process_new "$SOURCE"' in late
    assert "research-digest" not in late


def test_estimate_command_reads_a_listing_and_does_not_need_the_database(monkeypatch, tmp_path, capsys):
    sn = "e" * 32
    listing = tmp_path / "post-history.jsonl"
    listing.write_text(json.dumps({"sn": sn, "title": "一篇", "skip": ""}) + "\n", encoding="utf-8")

    def boom(*args, **kwargs):
        raise AssertionError("不该连库，也不该调模型")

    monkeypatch.setattr(llm_cost, "load_db_rows", boom)
    assert llm_cost.main(["--listing", str(listing), "--chars-each", "2000"]) == 0
    out = capsys.readouterr().out
    assert "2,000" in out or "2000" in out
    assert "DeepSeek" in out
    assert "不调用模型" in out or "没有调用模型" in out
