"""译制闭环：能证明的数值错误拒绝，证明不了的不放行。不改实跑译文。"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from test_bilingual_regressions import MemoryConn

from researchlib.translate import service
from researchlib.translate.harness import apply_report, evidence_id, numeric_verdict, settle_numbers, settle_unshield
from researchlib.translate.llm import ChatJsonResult, _params
from researchlib.translate.provenance import AlignmentError, leaf_problem
from researchlib.translate.segments import shield

_LIVE = [
    (
        21998,
        (
            "<p data-src-para=\"1\">In the wake of "
            "the #MeToo movement, countless women"
            " have taken to Facebook to express t"
            "heir frustration and disappointment "
            "with men and have been promptly shut"
            " down or silenced, banned from the p"
            "latform for periods ranging from one"
            " to seven days.</p>"
        ),
        (
            "<p data-src-para=\"1\">在 #MeToo 运动之后，无"
            "数女性在 Facebook 上表达她们对男性的不满和失望，却迅速被封禁或"
            "禁言，被禁止使用该平台1至7天。</p>"
        ),
    ),
    (
        22065,
        (
            "<p>As the week draws to a close, it’"
            "s definitely been a long one for App"
            "le’s software engineers. The nightma"
            "re started on Tuesday, after a Twitt"
            "er user disclosed a critical securit"
            "y vulnerability for macOS High Sierr"
            "a that allowed anyone with physical "
            "access to a Mac to gain system admin"
            "istrator privileges without even ent"
            "ering a password. As software bugs g"
            "o, this one was embarrassing and cri"
            "tical, but Apple managed to fix it l"
            "ess than 24 hours after it was publi"
            "cly disclosed.<br> “Security is a to"
            "p priority for every Apple product, "
            "and regrettably we stumbled with thi"
            "s release of macOS,” admitted an App"
            "le spokesperson. “We greatly regret "
            "this error and we apologize to all M"
            "ac users, both for releasing with th"
            "is vulnerability and for the concern"
            " it has caused. Our customers deserv"
            "e better. We are auditing our develo"
            "pment processes to help prevent this"
            " from happening again.”</p>"
        ),
        (
            "<p>随着本周接近尾声，对苹果的软件工程师来说，这无疑是漫长的一周。噩梦"
            "始于周二：一名 Twitter 用户披露了 macOS High Sie"
            "rra 的一个严重安全漏洞，任何人只要能够物理接触一台 Mac，甚至无需"
            "输入密码，就能获得系统管理员权限。就软件缺陷而言，这个漏洞既令人难堪又极"
            "其严重，但苹果在漏洞公开披露后不到 24 小时内就完成了修复。<br>“"
            "安全是每一款苹果产品的首要任务，遗憾的是我们在这次 macOS 发布上出"
            "了差错。”苹果发言人承认道，“我们对这一错误深表遗憾，并向所有 Mac "
            "用户致歉，既为发布时带有这一漏洞，也为它造成的担忧。我们的用户理应得到更"
            "好的产品。我们正在审查开发流程，以帮助防止此类事件再次发生。” </p>"
        ),
    ),
    (
        21985,
        (
            "<p>Beginning in the late 90s two new"
            " broadband technologies began to bec"
            "ome available: cable modems and DSL "
            "over phone lines. DSL was a big impr"
            "ovement over dial-up, offering faste"
            "r speeds, always-on connectivity, th"
            "e ability to still use your phone li"
            "ne for voice calls, etc. And, keepin"
            "g with the established regulatory re"
            "gime for telephone lines, DSL servic"
            "es were also covered by Title II, wh"
            "ich meant that multiple ISPs could u"
            "se the telephone lines. Cable modems"
            ", though, were unregulated (and thus"
            " ISP service was held exclusively by"
            " cable companies).</p>"
        ),
        (
            "<p>从20世纪90年代末开始，两种新的宽带技术逐渐面世：电缆调制解调器"
            "和通过电话线提供的DSL。DSL相比拨号上网是一大进步：速度更快、可始终"
            "在线，还能继续用电话线打语音电话，等等。而且，按照电话线路既有的监管制度"
            "，DSL服务也受Title II约束，这意味着多家ISP可以使用电话线路"
            "。不过，电缆调制解调器不受监管（因此ISP服务完全由有线电视公司把持）。"
            "</p>"
        ),
    ),
    (
        21934,
        (
            "<p>This ethos pervades every part of"
            " Meredith, from editorial content (c"
            "urated by title to multiple demograp"
            "hics) to business models (circulatio"
            "n plus ads plus content marketing pl"
            "us the second most valuable brand li"
            "censing program after Disney) to bac"
            "k-end integration (Meredith has an a"
            "verage of 800 data points on 100 mil"
            "lion American women). The company ma"
            "y look like a publisher and call its"
            "elf a marketer, but, to <a href=\"htt"
            "p://www.berkshirehathaway.com/letter"
            "s/1991.html\">reference a Warren Buff"
            "ett letter</a> I’ve <a href=\"../2016"
            "/how-the-snap-spectacles-story-broke"
            "-facebook-overestimated-video-metric"
            ".md\">referenced</a> <a href=\"espns-c"
            "uts-espns-history-espn-from-franchis"
            "e-to-business.md\">before</a>, Meredi"
            "th is a <em>business</em>:</p>"
        ),
        (
            "<p>这种理念渗透到梅雷迪思的每个部分，从编辑内容（按刊物针对不同人群精"
            "心策划）到商业模式（发行量加广告加内容营销，再加上仅次于迪士尼的全球第二"
            "值钱的品牌授权项目），再到后端整合（梅雷迪思对1亿美国女性平均掌握800"
            "个数据点）。这家公司看起来像出版商，也自称营销公司，但借用<a href"
            "=\"http://www.berkshirehathaway.com/l"
            "etters/1991.html\">沃伦·巴菲特的一封信</a>——我<"
            "a href=\"../2016/how-the-snap-spectac"
            "les-story-broke-facebook-overestimat"
            "ed-video-metric.md\">之前</a>曾<a href=\""
            "espns-cuts-espns-history-espn-from-f"
            "ranchise-to-business.md\">引用</a>过——的话"
            "来说，梅雷迪思是一门<em>生意</em>：</p>"
        ),
    ),
    (
        22054,
        (
            "<p>Last year Turner made about $0.80"
            " in advertising revenue for every $1"
            " in subscription revenue; applying t"
            "hat ratio to our hypothetical market"
            " means a further $2.9 million in hig"
            "h margin advertising revenue per mon"
            "th.</p>"
        ),
        (
            "<p>去年，特纳每获得1美元订阅收入，就产生约0.80美元的广告收入；把"
            "这个比例套用到我们假设的市场，意味着每月还能带来290万美元的高利润率广"
            "告收入。</p>"
        ),
    ),
]


@pytest.mark.parametrize(
    ("article_id", "source", "target", "verdict"),
    [
        (*row, expected)
        for row, expected in zip(
            _LIVE,
            ["conflict", "conflict", "uncertain", "uncertain", "conflict"],
            strict=True,
        )
    ],
)
def test_live_v6_slots_keep_their_saved_wording(article_id, source, target, verdict):
    assert leaf_problem(source, target) == "numbers"
    assert numeric_verdict(source, target) == verdict
    assert article_id


@pytest.mark.parametrize(
    ("source", "target", "verdict"),
    [
        ("<p>Profit was 10 dollars.</p>", "<p>利润为20美元。</p>", "conflict"),
        ("<p>The deal was $1.85 billion.</p>", "<p>交易是1.85亿美元。</p>", "scale"),
        ("<p>It took seven months.</p>", "<p>花了七周。</p>", "conflict"),
        ("<p>Sales were $10-15 billion.</p>", "<p>销售额为10亿至15亿美元。</p>", "scale"),
        (
            "<p>Profit was 10 dollars then 20 dollars.</p>",
            "<p>利润先是20美元，然后是10美元。</p>",
            "binding",
        ),
        ("<p>The call is at 12:15AM.</p>", "<p>电话在12:15PM。</p>", "conflict"),
        ("<p>Shipped in November 2014.</p>", "<p>2014年12月发出。</p>", "conflict"),
        ("<p>Demand was fiscal Q4.</p>", "<p>需求是第3财季。</p>", "conflict"),
    ],
)
def test_clear_number_errors_stay_conflicts(source, target, verdict):
    assert numeric_verdict(source, target) == verdict


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("<p>less than 24 hours</p>", "<p>不到 24 天</p>"),
        ("<p>from one to seven days</p>", "<p>1至8天</p>"),
        ("<p>from one to seven days</p>", "<p>1至7周</p>"),
        ("<p>$0.80 for every $1.</p>", "<p>每获得0.80美元，就产生约1美元。</p>"),
        ("<p>$0.80 after 7 months for every $1.</p>", "<p>每获得1美元就产生0.80美元。</p>"),
        (
            "<p>Revenue went from $200 billion to $220 billion.</p>",
            "<p>收入从2200亿美元降至2000亿美元。</p>",
        ),
        ("<p>800 data points on 100 million women.</p>", "<p>对8亿人掌握100个数据点。</p>"),
        (
            "<p>Revenue was 10 million and costs were 20 million.</p>",
            "<p>收入2000万，成本1000万。</p>",
        ),
        ("<p>in the late 90s</p>", "<p>20世纪80年代末</p>"),
    ],
)
def test_bound_facts_do_not_collapse_to_a_number_set(source, target):
    assert numeric_verdict(source, target) != "ok"


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("<p>It took 60 days.</p>", "<p>花了60 天。</p>"),
        ("<p>less than 24 hours</p>", "<p>不到 24 小时</p>"),
        ("<p>The call is at 12:15AM.</p>", "<p>电话在凌晨12:15。</p>"),
        ("<p>Shipped in November 2014.</p>", "<p>2014年11月发出。</p>"),
        ("<p>Demand was fiscal Q4.</p>", "<p>需求是第4财季。</p>"),
        (
            "<p>Revenue was 10 million and costs were 20 million.</p>",
            "<p>收入1000万，成本2000万。</p>",
        ),
        ("<p>$10 per person or $20 per household.</p>", "<p>每位10美元或每户20美元。</p>"),
        ("<p>$0.80 for every $1.</p>", "<p>每1美元就有0.80美元。</p>"),
    ],
)
def test_same_binding_accepts_format_and_argument_order(source, target):
    assert numeric_verdict(source, target) == "ok"


def test_repair_is_kept_only_when_the_checker_passes(tmp_path, monkeypatch):
    def fake(**kwargs):
        if kwargs["user"].get("verify"):
            return ChatJsonResult(
                data={"evidence_id": kwargs["user"]["evidence_id"], "verdict": "uncertain", "claims": []},
                model="synthetic-model",
                usage=None,
                latency_ms=0,
            )
        assert "不要自己宣布通过" in kwargs["user"]["repair"]
        return ChatJsonResult(
            data={"t": ["<p>利润为10美元。</p>"]},
            model="synthetic-model",
            usage=None,
            latency_ms=0,
        )

    monkeypatch.setattr("researchlib.translate.service.chat_json", fake)
    text, code, _stages = settle_numbers("<p>Profit was 10 dollars.</p>", "<p>利润为20美元。</p>", tmp_path)
    assert code is None and "10美元" in text


def test_repeating_a_wrong_amount_is_still_rejected(tmp_path, monkeypatch):
    def fake(**kwargs):
        user = kwargs["user"]
        if user.get("verify"):
            return ChatJsonResult(
                data={
                    "evidence_id": user["evidence_id"],
                    "verdict": "conflict",
                    "claims": [{"source": "10 dollars", "target": "20美元", "same": False}],
                },
                model="synthetic-model",
                usage=None,
                latency_ms=0,
            )
        return ChatJsonResult(
            data={"t": ["<p>利润为20美元。</p>"]},
            model="synthetic-model",
            usage=None,
            latency_ms=0,
        )

    monkeypatch.setattr("researchlib.translate.service.chat_json", fake)
    text, code, stages = settle_numbers("<p>Profit was 10 dollars.</p>", "<p>利润为20美元。</p>", tmp_path)
    assert code == "numbers" and "20美元" in text
    assert "verify" in [item["step"] for item in stages]
    assert stages[-1]["status"] == "contradict"


def test_same_numbers_on_swapped_objects_are_binding():
    assert numeric_verdict(
        "<p>Revenue was 10 million and costs were 20 million.</p>",
        "<p>收入2000万，成本1000万。</p>",
    ) == "binding"


_GROUNDED = {
    21998: [{"source": "one to seven days", "target": "1至7天", "same": True}],
    22065: [
        {"source": "As the week", "target": "本周", "same": True},
        {"source": "a long one", "target": "漫长的一周", "same": True},
        {"source": "less than 24 hours", "target": "不到 24 小时", "same": True},
    ],
    21985: [
        {"source": "late 90s", "target": "20世纪90年代末", "same": True},
        {"source": "two", "target": "两种", "same": True},
    ],
    21934: [
        {
            "source": "800 data points", "target": "800个数据点",
            "object_source": "data points", "object_target": "数据点", "same": True,
        },
        {
            "source": "100 million American women", "target": "1亿美国女性",
            "object_source": "women", "object_target": "女性", "same": True,
        },
    ],
    22054: [
        {
            "source": "$0.80 in advertising revenue for every $1",
            "target": "每获得1美元订阅收入，就产生约0.80美元",
            "same": True,
        },
        {"source": "$2.9 million", "target": "290万美元", "same": True},
    ],
}


def _equivalent(source: str, target: str, claims: list[dict]) -> dict:
    return {"evidence_id": evidence_id(source, target), "verdict": "equivalent", "claims": claims}


def test_saved_live_slots_pass_when_quotes_ground_the_same_facts(tmp_path, monkeypatch):
    for article_id, source, target in _LIVE:
        found = apply_report(source, target, _equivalent(source, target, _GROUNDED[article_id]))
        assert found.status == "accept", article_id
        bare = apply_report(source, target, {
            "evidence_id": evidence_id(source, target), "verdict": "equivalent", "claims": [],
        })
        assert bare.status == "uncertain", article_id

    article_id, source, target = _LIVE[0]

    def fake(**kwargs):
        assert kwargs["user"].get("verify")
        return ChatJsonResult(
            data=_equivalent(source, target, _GROUNDED[article_id]),
            model="synthetic-model",
            usage=None,
            latency_ms=0,
        )

    monkeypatch.setattr("researchlib.translate.service.chat_json", fake)
    text, code, stages = settle_numbers(source, target, tmp_path)
    assert code is None and text == target and stages[0]["step"] == "verify"


def test_grounded_conflicts_block_time_scale_and_binding():
    samples = [
        ("<p>The call is at 12:15AM.</p>", "<p>电话在12:15PM。</p>",
         [{"source": "12:15AM", "target": "12:15PM", "same": True}], "equivalent"),
        ("<p>Shipped in November 2014.</p>", "<p>2014年12月发出。</p>",
         [{"source": "November 2014", "target": "2014年12月", "same": False}], "conflict"),
        ("<p>Demand was fiscal Q4.</p>", "<p>需求是第3财季。</p>",
         [{"source": "fiscal Q4", "target": "第3财季", "same": False}], "conflict"),
        ("<p>The deal was $1.85 billion.</p>", "<p>交易是1.85亿美元。</p>",
         [{"source": "$1.85 billion", "target": "1.85亿美元", "same": True}], "equivalent"),
        ("<p>Revenue was 10 million and costs were 20 million.</p>", "<p>收入2000万，成本1000万。</p>", [
            {"source": "Revenue was 10 million", "target": "收入2000万",
             "object_source": "Revenue", "object_target": "收入", "same": False},
        ], "conflict"),
        ("<p>800 data points on 100 million women.</p>", "<p>对800人掌握1亿个数据点。</p>", [
            {"source": "800 data points", "target": "800人",
             "object_source": "data points", "object_target": "人", "same": False},
            {"source": "100 million women", "target": "1亿个数据点",
             "object_source": "women", "object_target": "数据点", "same": False},
        ], "conflict"),
    ]
    for source, target, claims, verdict in samples:
        found = apply_report(source, target, {
            "evidence_id": evidence_id(source, target), "verdict": verdict, "claims": claims,
        })
        assert found.status == "contradict", (source, found)


def test_repair_with_the_wrong_source_hash_is_discarded(tmp_path, monkeypatch):
    def fake(**kwargs):
        user = kwargs["user"]
        if user.get("verify"):
            return ChatJsonResult(
                data={"evidence_id": user["evidence_id"], "verdict": "uncertain", "claims": []},
                model="synthetic-model",
                usage=None,
                latency_ms=0,
            )
        return ChatJsonResult(
            data={"t": [{
                "id": user["segment_ids"][0],
                "source_hash": "not-the-source",
                "html": "<p>利润为10美元。</p>",
            }]},
            model="synthetic-model",
            usage=None,
            latency_ms=0,
        )

    monkeypatch.setattr("researchlib.translate.service.chat_json", fake)
    text, code, stages = settle_numbers(
        "<p>Profit was 10 dollars.</p>",
        "<p>利润为20美元。</p>",
        tmp_path,
        segment_id="seg-1",
        source_hash="source-hash",
    )
    assert "20美元" in text and code
    assert stages[-1]["reason"] == "response-source-hash"
    assert stages[-1]["rejected"] == "<p>利润为10美元。</p>"

    def missing_id(**kwargs):
        if kwargs["user"].get("verify"):
            return ChatJsonResult(
                data={"evidence_id": kwargs["user"]["evidence_id"], "verdict": "uncertain", "claims": []},
                model="synthetic-model",
                usage=None,
                latency_ms=0,
            )
        return ChatJsonResult(
            data={"t": [{"html": "<p>利润为10美元。</p>", "source_hash": "source-hash"}]},
            model="synthetic-model",
            usage=None,
            latency_ms=0,
        )

    monkeypatch.setattr("researchlib.translate.service.chat_json", missing_id)
    text, code, stages = settle_numbers(
        "<p>Profit was 10 dollars.</p>",
        "<p>利润为20美元。</p>",
        tmp_path,
        segment_id="seg-1",
        source_hash="source-hash",
    )
    assert "20美元" in text and stages[-1]["reason"] == "response-identity-missing"


def test_deepseek_request_drops_the_repair_temperature(monkeypatch):
    monkeypatch.delenv("RESEARCH_LLM_PROVIDER", raising=False)
    assert "temperature" not in _params(0.3, 800)


def test_missing_placeholder_is_repaired_once(tmp_path, monkeypatch):
    shielded = shield("<p>See <code>rate = 42</code>.</p>")
    calls = []

    def fake(**kwargs):
        calls.append(kwargs["user"]["repair"])
        return ChatJsonResult(
            data={"t": [shielded.html]},
            model="synthetic-model",
            usage=None,
            latency_ms=0,
        )

    monkeypatch.setattr("researchlib.translate.service.chat_json", fake)
    restored, stages = settle_unshield(shielded.html, "<p>见图。</p>", shielded, tmp_path)
    assert restored and "rate = 42" in restored and len(calls) == 1
    assert stages[-1]["step"] == "repair" and stages[-1]["output"] == shielded.html
    assert "不要自己宣布通过" in calls[0]


def test_placeholder_still_missing_is_not_retried(tmp_path, monkeypatch):
    shielded = shield("<p>See <code>rate = 42</code>.</p>")
    calls = []

    def fake(**kwargs):
        calls.append(1)
        return ChatJsonResult(
            data={"t": ["<p>见图。</p>"]},
            model="synthetic-model",
            usage=None,
            latency_ms=0,
        )

    monkeypatch.setattr("researchlib.translate.service.chat_json", fake)
    restored, stages = settle_unshield(shielded.html, "<p>见图。</p>", shielded, tmp_path)
    assert restored is None and len(calls) == 1
    assert stages[-1]["output"] == "<p>见图。</p>"
    assert stages[-1]["reason"] == "repair still did not restore"


def test_a_rejected_article_does_not_store_a_translation(tmp_path):
    body = "<p>Acme earned 10 dollars from new subscribers while the platform kept its distribution.</p>"
    conn = MemoryConn(body)
    calls = []

    def fake(**kwargs):
        user = kwargs["user"]
        calls.append(user)
        if user.get("verify"):
            return ChatJsonResult(
                data={"evidence_id": user["evidence_id"], "verdict": "uncertain", "claims": []},
                model="synthetic-model",
                usage=None,
                latency_ms=0,
            )
        if user.get("segments") == ["Synthetic audit"]:
            values = ["合成标题"]
        else:
            values = ["<p>甲公司从新订户赚了20美元，仍保持平台分发。</p>"]
        if "segment_ids" in user:
            values = [
                {"id": identity, "source_hash": source_hash, "html": text}
                for identity, source_hash, text in zip(
                    user["segment_ids"], user["source_hashes"], values, strict=True,
                )
            ]
        return ChatJsonResult(data={"t": values}, model="synthetic-model", usage=None, latency_ms=0)

    with patch.object(service, "chat_json", fake), patch.object(service, "replace_translation_chunks"):
        with pytest.raises(AlignmentError, match="numbers"):
            service.translate_article(conn, 990001, data_dir=tmp_path)
    assert conn.writes == [] and conn.committed is False
    assert any("repair" in call for call in calls)
    assert any(call.get("verify") for call in calls)
    saved = (tmp_path / "translate-rejects" / "990001.jsonl").read_text(encoding="utf-8")
    assert "10 dollars" in saved and "20美元" in saved
    assert '"step": "repair"' in saved and "DEEPSEEK" not in saved and "api_key" not in saved.lower()
