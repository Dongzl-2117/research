"""A source nickname is display metadata, never an account credential."""

from test_embedding_provenance import current_row

from researchlib.author_voice import assign_poster_role


def test_matching_zsxq_alias_without_account_stays_unknown():
    meta = {"poster_name": "display alias"}
    assign_poster_role(meta, "author", ("display alias",), source_id="fangwei-zsxq", source_kind="zsxq")
    assert meta["poster_role"] == "unknown"


def test_verified_zsxq_account_does_not_require_matching_display_name():
    meta = current_row()["raw_meta"]
    meta["poster_name"] = "different display name"
    assign_poster_role(meta, "author", ("display alias",), source_id="fangwei-zsxq", source_kind="zsxq")
    assert meta["poster_role"] == "owner"
