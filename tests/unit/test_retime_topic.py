from researchlib.retime import topic_id_from_external_id


def test_dated_external_id_uses_topic_suffix():
    assert topic_id_from_external_id("2024-11-28_888888") == "888888"
    assert topic_id_from_external_id("888888") == "888888"
    assert topic_id_from_external_id("md/2024-11-28_888888.md") == "888888"
    assert topic_id_from_external_id("INDEX") is None
    assert topic_id_from_external_id("INDEX.md") is None
    assert topic_id_from_external_id("2024-11-28_INDEX") is None
    assert topic_id_from_external_id("") is None
