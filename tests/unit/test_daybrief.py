from researchlib.web.daybrief import compose_day_brief


def test_day_brief_is_filled_without_a_model():
    brief = compose_day_brief(
        [
            {
                "id": 1,
                "title": "腾讯的广告",
                "author": "方伟",
                "summary": "广告业务是这篇的主体。",
                "excerpt": "",
                "time": "13:29",
                "iso": "2024-10-19T13:29:00+08:00",
                "tags": [{"id": 7, "name": "腾讯"}],
            },
            {
                "id": 2,
                "title": "另一篇",
                "author": "乘风破浪",
                "summary": "",
                "excerpt": "只剩正文开头。",
                "time": "",
                "iso": "2024-10-19",
                "tags": [],
            },
        ]
    )
    assert brief["lead"].startswith("方伟：广告业务是这篇的主体。")
    assert "乘风破浪：" in brief["lead"]
    assert brief["authors"] == 2
    assert brief["sections"][0]["items"][0]["blurb"] == "广告业务是这篇的主体。"
    assert brief["companies"] == [{"id": 7, "name": "腾讯"}]
    assert compose_day_brief([])["lead"] == ""
