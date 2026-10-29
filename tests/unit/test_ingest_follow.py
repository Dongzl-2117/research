"""导入之后只排这次写入的带图文章和英文文章。"""

from researchlib.ingest.follow import INGEST_TRANSLATE_LIMIT, plan_followups


def test_plan_splits_images_and_english():
    rows = [
        {
            "id": 1,
            "body_text": "这是一段中文正文，讲公司和生意，没有英文段落。",
            "body_md": "",
            "raw_meta": {},
            "has_images": True,
        },
        {
            "id": 2,
            "body_text": "This is an English essay about platforms and aggregation theory.",
            "body_md": "",
            "raw_meta": {"language": "en"},
            "has_images": True,
        },
        {
            "id": 3,
            "body_text": "Another English note about cloud revenue and margins this quarter.",
            "body_md": "",
            "raw_meta": {},
            "has_images": False,
        },
    ]
    plan = plan_followups(rows)
    assert plan["ocr"] == [1, 2]
    assert plan["translate"] == [2, 3]


def test_plan_caps_are_applied_by_the_caller_constants():
    rows = [
        {
            "id": index,
            "body_text": "English paragraph about a company and its margins for the year.",
            "body_md": "",
            "raw_meta": {"language": "en"},
            "has_images": True,
        }
        for index in range(INGEST_TRANSLATE_LIMIT + 3)
    ]
    plan = plan_followups(rows)
    assert len(plan["translate"]) == INGEST_TRANSLATE_LIMIT + 3
    assert plan["translate"][:INGEST_TRANSLATE_LIMIT] == list(range(INGEST_TRANSLATE_LIMIT))
