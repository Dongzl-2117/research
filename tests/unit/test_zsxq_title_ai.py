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
    assert accept_ai_title("存在运用杠杆") == "存在运营杠杆"
    assert accept_ai_title("很长" * 30) is None
    # 24002：评论列表残屑不能当模型给出的标题。
    live = "泡泡玛特： 泡泡玛特： 评论（1） - 流年（2026-10-28 09"
    assert accept_ai_title(live) is None


def test_accept_ai_title_keeps_paired_marks_at_the_edges():
    """只去包住整行的引号。开头的书名号配得上对，是标题的一部分。"""
    assert accept_ai_title("“腾讯的护城河还在”") == "腾讯的护城河还在"
    assert accept_ai_title("《穷查理宝典》读后感") == "《穷查理宝典》读后感"
    assert accept_ai_title("“腾讯”的护城河还在") == "“腾讯”的护城河还在"
    assert accept_ai_title("腾讯的护城河还在（上）") == "腾讯的护城河还在（上）"
    assert accept_ai_title("“估值越低，回购越划算") == "估值越低，回购越划算"
