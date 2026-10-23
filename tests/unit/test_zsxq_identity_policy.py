"""Identity regressions use synthetic text with the verified account binding."""

import copy
import json
from pathlib import Path

import pytest

from researchlib.author_voice import for_model
from researchlib.eligibility import (
    IDENTITY_KEY,
    chunk_cache_safe_article,
    chunk_cache_safe_sql,
    eligibility_sql,
    identity_for_topic,
    is_eligible_article,
    model_cache_safe_article,
    project_article,
    topic_comments,
)
from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.parse import author_replies, parse_archive
from researchlib.ingest.store import _store_one
from researchlib.sources import Source

OWNER = "1825524112"
GROUP = "48888181884848"
SOURCE = "fangwei-zsxq"


def topic(poster="7", comments=None, group=GROUP):
    return {
        "topic_id": "123456", "group": {"group_id": group},
        "talk": {"owner": {"user_id": poster, "name": "方伟"}, "text": "guest thesis"},
        "comments": comments or [],
    }


def reply(cid="2", author=OWNER, recipient="7", parent="1", text="verified owner reply"):
    return {
        "comment_id": cid, "owner": {"user_id": author, "name": "方伟"},
        "repliee": {"user_id": recipient}, "parent_comment_id": parent, "text": text,
    }


def article(node, **extra):
    return {
        "id": 17, "source_id": SOURCE, "title": "guest headline", "body_md": "guest thesis ![](guest.png)",
        "raw_meta": {IDENTITY_KEY: identity_for_topic(node, SOURCE)}, **extra,
    }


def test_poster_and_recipient_are_not_owner_evidence():
    guest = topic(comments=[reply(author="7", recipient=OWNER)])
    assert author_replies(guest) == []
    assert not is_eligible_article(article(guest))
    assert identity_for_topic(guest, SOURCE)["kind"] == "excluded"


@pytest.mark.parametrize("change", ["source", "group", "owner", "version", "poster"])
def test_binding_cannot_be_reused_under_another_identity(change):
    row = article(topic(poster=OWNER))
    identity = row["raw_meta"][IDENTITY_KEY]
    if change == "source":
        row["source_id"] = "chengfengpolang"
    else:
        identity[{"group": "group_id", "owner": "owner_account_id", "version": "version",
                  "poster": "poster_account_id"}[change]] = "wrong"
    assert not is_eligible_article(row)
    assert project_article(row) is None


def test_full_comments_override_preview_and_nested_parent_is_preserved():
    node = topic(comments=[
        {"comment_id": "1", "owner": {"user_id": "7"}, "text": "real question?",
         "replied_comments": [reply(parent="")]},
    ])
    node["show_comments"] = [reply(cid="wrong", author="7", text="preview only")]
    flattened = topic_comments(node)
    assert [c["comment_id"] for c in flattened] == ["1", "2"]
    assert flattened[1]["parent_comment_id"] == "1"
    assert author_replies(node) == [("real question?", "verified owner reply")]


def test_recipient_id_does_not_guess_a_parent_question():
    node = topic(comments=[
        {"comment_id": "1", "owner": {"user_id": "7"}, "text": "earlier question?"},
        reply(parent=""),
    ])
    assert author_replies(node) == [("", "verified owner reply")]


def test_guest_projection_keeps_only_verified_replies_and_explicit_question():
    node = topic(comments=[
        {"comment_id": "1", "owner": {"user_id": "7"}, "text": "question ![](guest.png)?"},
        reply(), reply(cid="3", author="8", text="unrelated guest opinion"),
    ])
    row = article(node, images=[{"storage_path": "guest.png"}], digest={"summary": "guest thesis"})
    projected = project_article(row)
    assert projected is not None
    assert "guest thesis" not in projected["body_md"]
    assert "unrelated guest opinion" not in projected["body_md"]
    assert "guest.png" not in projected["body_md"]
    assert "网友问题（仅作星主回复上下文）" in projected["body_md"]
    assert "verified owner reply" in projected["body_md"]
    assert projected["images"] == [] and projected["digest"] is None
    assert projected["title"] == "星主回复：verified owner reply"
    assert row["body_md"] == "guest thesis ![](guest.png)"
    text, _chunks, restricted = for_model("guest thesis", [], row["raw_meta"], source_id=SOURCE)
    assert text == "verified owner reply" and restricted


def test_legacy_real_account_is_eligible_but_cache_and_nicknames_are_not():
    row = {"source_id": SOURCE, "body_md": "owner body\n\n## 评论（1）\n\nspoofed owner comment",
           "raw_meta": {"poster_id": OWNER, "poster_name": "任意显示名", "poster_role": "guest"}}
    assert is_eligible_article(row)
    assert not model_cache_safe_article(row)
    assert project_article(row)["body_md"].strip() == "owner body"
    for poster in ("7", "", "418551555845518"):
        other = copy.deepcopy(row)
        other["raw_meta"].update(poster_id=poster, poster_name="方伟", poster_role="owner")
        assert not is_eligible_article(other)
    conflict = copy.deepcopy(row)
    conflict["raw_meta"]["group_id"] = "other group"
    assert not is_eligible_article(conflict)


def test_legacy_projection_clears_flattened_cache_even_when_body_is_unchanged():
    row = {"source_id": SOURCE, "title": "owner title", "body_md": "owner body",
           "raw_meta": {"poster_id": OWNER}, "digest_summary": "unproven cached model content",
           "digest_points": ["unproven points"]}
    projected = project_article(row)
    assert projected["digest_summary"] is None and projected["digest_points"] == []
    assert projected["body_md"] == "owner body"


def test_new_projection_cache_requires_exact_stored_body():
    row = article(topic(comments=[reply(parent="")]))
    projected = project_article(row)
    assert projected is not None
    projected["raw_meta"][IDENTITY_KEY]["projection_md"] = projected["body_md"]
    assert model_cache_safe_article(projected)
    projected["body_md"] += "\nold guest text"
    assert not model_cache_safe_article(projected)


def test_non_zsxq_sources_and_sql_alias_contract():
    assert is_eligible_article({"source_id": "fangwei-mp", "raw_meta": {}})
    assert model_cache_safe_article({"source_id": "fangwei-mp", "raw_meta": {}})
    assert ".raw_meta" not in eligibility_sql("")
    assert "_elig_reply->>'author_account_id'" in eligibility_sql("a")
    with pytest.raises(ValueError):
        eligibility_sql("a; DROP TABLE articles")


@pytest.mark.parametrize("version", [True, False, "1", 1.0, None])
def test_identity_version_is_a_json_integer_not_bool_or_string(version):
    row = article(topic(poster=OWNER))
    row["raw_meta"][IDENTITY_KEY]["version"] = version
    assert not is_eligible_article(row)
    assert "->'version' = '1'::jsonb" in eligibility_sql()


def test_legacy_base_chunks_need_exact_safe_owner_text():
    row = {"source_id": SOURCE, "title": "owner title",
           "body_md": "own investment thesis\n\n## 评论（1）\n\nforged commenter thought",
           "body_text": "own investment thesis\n\n评论（1）\n\nforged commenter thought",
           "raw_meta": {"poster_id": OWNER}}
    assert chunk_cache_safe_article(row, {"body": "own investment thesis", "heading": "owner title", "ord": 1})
    assert not chunk_cache_safe_article(row, {"body": "forged commenter thought", "ord": 2})
    assert not chunk_cache_safe_article(row, {"body": "own investment thesis", "ord": 10001})
    assert not chunk_cache_safe_article(row, {"body": "own investment thesis", "ord": 1, "lang": "zh"})
    assert "regexp_split_to_array" in chunk_cache_safe_sql()


def test_comment_quarantine_preserves_following_body_and_fenced_examples():
    from researchlib.eligibility import strip_unverified_comments
    from researchlib.zsxq_comments import filter_host_comments_markdown

    md = (
        "owner before\n\n## 评论（1）\n\n- **方伟**（2026-01-01 12:00）\n  unverified"
        "\n\n## Owner appendix\n\nowner after"
    )
    cleaned = filter_host_comments_markdown(md)
    assert "unverified" not in cleaned and "owner before" in cleaned and "owner after" in cleaned
    fenced = "```md\n## 评论（1）\nthis is code\n```\n\nowner continues"
    assert strip_unverified_comments(fenced) == fenced


def test_canonical_owner_reply_projection_is_idempotent_and_renders():
    from researchlib.render import render_body

    row = article(topic(poster=OWNER, comments=[reply(parent="")]), body_md="real owner body")
    projected = project_article(row)
    repeated = project_article(projected)
    assert repeated["body_md"] == projected["body_md"]
    assert repeated["body_md"].count("verified owner reply") == 1
    assert "账户已核实" in str(render_body(repeated["body_md"], {}))
    assert "verified owner reply" in str(render_body(repeated["body_md"], {}))


def test_store_rejects_unknown_before_touching_database(tmp_path):
    class NoDatabase:
        def execute(self, *_args, **_kwargs):
            raise AssertionError("rejected content reached database")

    source = Source(id=SOURCE, name="test", author="方伟", kind="zsxq", enabled=True)
    doc = ParsedDoc("12345", "spoof", None, "guest body", {"poster_name": "方伟", "poster_id": "7"}, [])
    assert _store_one(NoDatabase(), source, doc, tmp_path, None) == ("rejected", 0, 0, 0, None)


def test_archive_reply_projection_drops_guest_images_and_keeps_id_evidence(tmp_path: Path):
    (tmp_path / "raw").mkdir()
    (tmp_path / "md").mkdir()
    (tmp_path / "images").mkdir()
    (tmp_path / "images" / "guest.png").write_bytes(b"synthetic image")
    (tmp_path / "raw" / "123456.json").write_text(json.dumps(topic(comments=[reply(parent="")])))
    (tmp_path / "md" / "123456.md").write_text("guest long thesis ![](../images/guest.png)")
    docs, errors, _files, _images = parse_archive(tmp_path, tmp_path / "work", id_mode="zsxq", source_id=SOURCE)
    assert not errors and len(docs) == 1
    doc = docs[0]
    assert "guest long thesis" not in doc.body_md and not doc.image_refs
    evidence = doc.raw_meta[IDENTITY_KEY]
    assert evidence["source_id"] == SOURCE and evidence["group_id"] == GROUP
    assert evidence["topic_id"] == "123456" and evidence["replies"][0]["comment_id"] == "2"
    assert evidence["projection_md"] == doc.body_md
