"""站内 flash 起题：校验输出，不打真接口。"""

from researchlib.zsxq_title_ai import accept_ai_title


def test_accept_ai_title_rejects_shells_placeholders_and_ellipsis():
    assert accept_ai_title("泡泡玛特下跌是自身问题，关键看一年底线") == "泡泡玛特下跌是自身问题，关键看一年底线"
    assert accept_ai_title("标题：西部矿业安全边际比巴菲特买中石油差很多") == "西部矿业安全边际比巴菲特买中石油差很多"
    assert accept_ai_title("「泡泡玛特：」") is None
    assert accept_ai_title("泡泡玛特：") is None
    assert accept_ai_title("无标题") is None
    assert accept_ai_title("护城河还在…") is None
    assert accept_ai_title("AI算力是一个很新都东西") == "AI算力是一个很新的东西"
    assert accept_ai_title("很长" * 30) is None
