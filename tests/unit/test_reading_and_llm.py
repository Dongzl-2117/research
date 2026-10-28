"""读原文：引用落到正文那一段。长文取段落。Grok 调用的重试、分批容错。论点地图的解析和对照。"""

import io
import json
import time
import urllib.error
from datetime import UTC, datetime, timedelta

import pytest

from researchlib import ask as ask_module
from researchlib import health, theses
from researchlib import llm as llm_module
from researchlib.chunks import make_chunks
from researchlib.config import Settings
from researchlib.excerpt import GAP, digest_input, views_input
from researchlib.llm import LLMError, chat
from researchlib.render import render_body
from researchlib.translate.pair import stamp_paragraph_ids
from researchlib.web.quoting import mark_quoted


def _article_html(body_md: str) -> str:
    return stamp_paragraph_ids(str(render_body(body_md, {})))


def test_quoted_chunk_is_marked_in_the_rendered_body():
    paragraphs = [
        f"第 {index} 段：腾讯第 {index} 个季度的广告收入和视频号库存都在变化，" + "具体的经营数据见财报。" * 18
        for index in range(1, 13)
    ]
    body = "## 广告\n\n" + "\n\n".join(paragraphs[:5]) + "\n\n![](pic.png)\n\n" + "\n\n".join(paragraphs[5:])
    chunks = make_chunks("腾讯", body)
    assert len(chunks) >= 3
    html = _article_html(body)
    target = chunks[1][1]
    marked, found = mark_quoted(html, target)
    assert found
    quoted = [block for block in marked.split("</p>") if "is-quoted" in block]
    lines = target.split("\n")
    # 标上的正好是这一块的几段：每段的开头都在块里，块的每段也都标上了。
    assert len(quoted) == len(lines)
    assert all(any(line[:12] in block for line in lines) for block in quoted)
    assert mark_quoted(html, "这段话正文里根本没有出现过，模型也不该编出来。") == (html, False)
    # 引用块跨过一张缺图的段落也接得上。
    across = next(text for _heading, text in chunks if "第 5 段" in text and "第 6 段" in text) if any(
        "第 5 段" in text and "第 6 段" in text for _heading, text in chunks
    ) else None
    if across is not None:
        marked, found = mark_quoted(html, across)
        assert found and marked.count("is-quoted") == len(across.split("\n"))


def test_short_paragraph_that_only_looks_like_the_start_is_skipped():
    real = "腾讯的广告业务这一季度继续增长，视频号的广告库存也在增加，商家投放往这里挪。"
    html = (
        "<p>腾讯的广告业务</p><p>别的内容完全不一样，讲的是美团的骑手补贴和配送成本。</p>"
        f"<p>{real}</p>"
    )
    marked, found = mark_quoted(html, real)
    assert found and marked.count("is-quoted") == 1
    assert f'<p class="is-quoted">{real}</p>' in marked
    # 块真的从一个小标题似的短段开始、后面接得上，照样标上。
    lead = "<p>一、广告业务</p><p>" + real + "</p>"
    marked, found = mark_quoted(lead, "一、广告业务\n" + real)
    assert found and marked.count("is-quoted") == 2


def test_digest_input_keeps_opening_ending_and_section_heads():
    chunks = [(f"第{index // 10}节", f"第 {index} 块。" + "字" * 590) for index in range(200)]
    body = "\n".join(text for _heading, text in chunks)
    text, reduced = digest_input(body, chunks, limit=30000)
    assert reduced and len(text) <= 32000
    assert "第 0 块。" in text and "第 199 块。" in text and GAP.strip() in text
    assert "## 第10节" in text  # 中间的小节开头也在
    short, cut = digest_input("短文。", [("", "短文。")], limit=30000)
    assert (short, cut) == ("短文。", False)


def test_views_input_follows_the_companies():
    chunks = [("", f"第 {index} 块讲别的。" + "字" * 600) for index in range(120)]
    chunks[90] = ("", "第 90 块：拼多多的 Temu 在美国被加了关税，半托管模式毛利被压。")
    body = "\n".join(text for _heading, text in chunks)
    text, reduced = views_input(body, chunks, ["拼多多", "PDD", "Temu"], limit=20000)
    assert reduced and "第 90 块：拼多多" in text and "第 91 块" in text
    assert "第 0 块" in text and "第 119 块" in text and "第 50 块" not in text


def _write_oauth(path, access="oauth-access") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "access_token": access,
                "refresh_token": "refresh-token",
                "token_type": "Bearer",
                "expires_at": time.time() * 1000 + 3_600_000,
                "obtained_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 60)),
                "needs_login": False,
                "last_error": None,
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def _no_shared_oauth(tmp_path, monkeypatch):
    # 别读到机器上真实的共用 OAuth 文件。留下 API key，确认调用不会拿它当凭证。
    monkeypatch.setenv("RESEARCH_LLM_PROVIDER", "grok")
    monkeypatch.setenv("XAI_OAUTH_TOKEN_PATH", str(tmp_path / "missing-oauth.json"))
    monkeypatch.setenv("XAI_API_KEY", "test-key-should-not-be-used")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("XAI_MAX_TOKENS", raising=False)
    monkeypatch.delenv("LLM_MAX_TOKENS", raising=False)


def _settings(tmp_path, monkeypatch) -> Settings:
    token = tmp_path / "secrets" / "xai-oauth.json"
    _write_oauth(token)
    monkeypatch.setenv("XAI_OAUTH_TOKEN_PATH", str(token))
    return Settings.from_env(
        {
            "DATABASE_URL": "postgresql://unused/research_test",
            "DATA_DIR": str(tmp_path),
            "RESEARCH_LLM_PROVIDER": "grok",
            "XAI_OAUTH_TOKEN_PATH": str(token),
        }
    )


def _http_error(code: int, headers=None) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://api.x.ai/v1/chat/completions", code, "err", headers, io.BytesIO(b"busy"))


def test_chat_retries_transient_errors_then_succeeds(tmp_path, monkeypatch):
    health.reset_llm_cache()
    calls = []
    outcomes = [_http_error(429), urllib.error.URLError(ConnectionResetError("reset")),
                {"choices": [{"message": {"content": "好"}}], "model": "grok-test"}]

    def fake_post(_settings, token, payload):
        calls.append((token, payload))
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    sleeps = []
    monkeypatch.setattr(llm_module, "_post", fake_post)
    monkeypatch.setattr(llm_module.time, "sleep", sleeps.append)
    assert chat(_settings(tmp_path, monkeypatch), [{"role": "user", "content": "hi"}]) == ("好", "grok-test")
    assert calls[-1][0] == "oauth-access"
    assert "test-key" not in calls[-1][0]
    assert len(calls) == 3 and sleeps == list(llm_module.RETRY_DELAYS[: len(sleeps)])
    health.reset_llm_cache()


def test_chat_does_not_retry_bad_requests_or_timeouts(tmp_path, monkeypatch):
    health.reset_llm_cache()
    settings = _settings(tmp_path, monkeypatch)
    for error in (_http_error(400), TimeoutError("read timed out"),
                  urllib.error.URLError(TimeoutError("connect timed out"))):
        calls = []

        def fake_post(_settings, _token, _payload, error=error, calls=calls):
            calls.append(1)
            raise error

        monkeypatch.setattr(llm_module, "_post", fake_post)
        monkeypatch.setattr(llm_module.time, "sleep", lambda _s: None)
        with pytest.raises(LLMError):
            chat(settings, [{"role": "user", "content": "hi"}])
        assert len(calls) == 1
    # 三次失败都记下来了，首页会提示。
    assert health.read_status(tmp_path, "llm")["failures"] == 3
    health.reset_llm_cache()


def test_synthesize_keeps_going_when_one_batch_fails(monkeypatch):
    import researchlib.ask as _ask
    monkeypatch.setattr(_ask, "SINGLE_CALL_CHARS", 64000)
    monkeypatch.setattr(_ask, "BATCH_CHARS", 48000)
    def fake_chat(_settings, messages, **_kwargs):
        system = messages[0]["content"]
        if system == ask_module.MAP_PROMPT:
            first = messages[1]["content"].split("资料：\n", 1)[1]
            number = int(first.split("]", 1)[0].lstrip("["))
            if number == 1:
                raise LLMError("Grok 接口返回 503：busy")
            return f"要点 [{number}]", "grok-test"
        return "汇总 [41]", "grok-test"

    monkeypatch.setattr(ask_module, "chat", fake_chat)
    items = [
        {"n": i, "title": f"第{i}篇", "author": "方伟", "date": "2025-01-01", "source": "s", "heading": "",
         "text": "字" * 700}
        for i in range(1, 121)
    ]
    report: dict = {}
    answer, _model = ask_module.synthesize(None, "问题", items, report)
    assert answer == "汇总 [41]" and report["failed_batches"] == [1]

    def always_fail(_settings, messages, **_kwargs):
        raise LLMError("Grok 接口返回 503：busy")

    monkeypatch.setattr(ask_module, "chat", always_fail)
    with pytest.raises(LLMError):
        ask_module.synthesize(None, "问题", items, {})


def _view(article_id, author, stance, days_ago, judgment="判断", quote=""):
    published = datetime(2026, 10, 1, tzinfo=UTC) - timedelta(days=days_ago)
    return {
        "article_id": article_id,
        "author": author,
        "stance": stance,
        "judgment": judgment,
        "mechanism": "",
        "risks": [],
        "falsifiers": [],
        "quote": quote,
        "content_hash": f"h{article_id}",
        "date": published.strftime("%Y-%m-%d"),
        "published_at": published,
    }


def test_theses_bucket_by_stance_and_drop_unrelated_or_neutral():
    # views_for 给的是新的在前。空白判断不进输入。
    views = [
        _view(14, "孤独进化体", "positive", 5, judgment="视频号广告库存还在涨", quote="视频号广告库存还在涨"),
        _view(13, "方伟", "negative", 10, judgment="关税把半托管的毛利吃掉了", quote="关税把半托管的毛利吃掉了"),
        _view(12, "乘风破浪", "negative", 100, judgment="Temu 的关税成本抬高，毛利承压", quote="Temu 的关税成本抬高"),
        _view(11, "方伟", "positive", 300, judgment="关税影响有限，Temu 毛利还能守住", quote="Temu 毛利还能守住"),
        _view(10, "方伟", "neutral", 1, judgment="关税和毛利先观察，Temu 先不说", quote="关税和毛利先观察"),
        _view(9, "方伟", "", 2, judgment="   "),
    ]
    real = [view for view in views if theses.is_real_view(view)]
    text, used = theses.build_input(real)
    assert text.index("[v11]") < text.index("[v14]") and len(used) == 5
    assert all(view["article_id"] != 9 for view in used)
    reply = (
        '{"theses": [{"title": "Temu 的关税风险", "mechanism": "半托管毛利被关税吃掉",'
        ' "for": ["v12", "v13", "v14", "v10", "v9", "v99"], "against": ["v11"]},'
        ' {"title": "只有一篇的论点", "views": ["v11"]}]}'
    )
    parsed = theses.parse_theses(reply, views, company="拼多多")
    # 边按判断和论点是否同向，不按对公司的总态度。毛利被吃掉的进支持，说还能守住的进反对。
    assert len(parsed) == 1
    assert parsed[0]["title"] == "Temu 的关税风险"
    assert set(parsed[0]["for"]) == {12, 13}
    assert parsed[0]["against"] == [11]
    assert parsed[0]["judgment"] and parsed[0]["sources"]
    shown = theses.present({"theses": parsed}, views, "拼多多")
    assert shown[0]["key"] == "t1" and shown[0]["for"]["count"] == 2 and shown[0]["against"]["count"] == 1
    assert shown[0]["against"]["latest"]["article_id"] == 11 and shown[0]["shifted"] == ["方伟"]
    assert sorted(shown[0]["articles"]) == [11, 12, 13] and shown[0]["quiet_quotes"] == []
    assert shown[0]["sources"] and shown[0]["sources"][0]["article_id"] in {11, 12, 13}
    with pytest.raises(LLMError):
        theses.parse_theses("没有 JSON", views)


def test_thesis_validator_rejects_mismatched_judgment_and_single_view():
    """茅台：定价权的判断不能挂在「普通飞天好卖」上；只有一篇的论点丢掉。"""
    views = [
        _view(654, "甲", "positive", 1, judgment="茅台具备逆周期的定价权，淡季也能守住批价",
              quote="逆周期的定价权让淡季批价也稳"),
        _view(1, "乙", "positive", 2, judgment="普通飞天动销很好，终端一直好卖", quote="普通飞天动销很好"),
        _view(2, "丙", "negative", 3, judgment="普通飞天并不好卖，动销在放缓", quote="普通飞天并不好卖"),
        _view(
            487, "丁", "positive", 4,
            judgment="AI 资本开支不影响茅台，飞天需求仍然稳",
            quote="AI 资本开支不影响茅台",
        ),
    ]
    reply = (
        '{"theses": [{"title": "普通飞天好卖", "mechanism": "终端动销强", "views": ["v654", "v1", "v2", "v487"]},'
        ' {"title": "逆周期定价权", "mechanism": "淡季也能守住批价", "views": ["v654"]}]}'
    )
    parsed = theses.parse_theses(reply, views, company="贵州茅台")
    assert len(parsed) == 1
    assert parsed[0]["for"] == [1] and parsed[0]["against"] == [2]
    assert parsed[0]["judgment"] and 654 not in parsed[0]["sources"]
    assert 654 not in parsed[0]["for"] and 487 not in parsed[0]["for"]


def test_thesis_hides_quote_that_does_not_support_the_title():
    views = [
        _view(1, "乙", "positive", 2, judgment="普通飞天动销很好，终端一直好卖",
              quote="逆周期的定价权让淡季批价也稳"),
        _view(2, "丙", "negative", 3, judgment="普通飞天并不好卖，动销在放缓", quote="普通飞天并不好卖"),
    ]
    reply = '{"theses": [{"title": "普通飞天好卖", "mechanism": "终端动销强", "views": ["v1", "v2"]}]}'
    parsed = theses.parse_theses(reply, views, company="贵州茅台")
    shown = theses.present({"theses": parsed}, views, "贵州茅台")
    assert shown[0]["quiet_quotes"] == [1]
    masked = theses.mask_unrelated_quotes(views, shown[0])
    assert next(view for view in masked if view["article_id"] == 1)["quote"] == ""
    assert next(view for view in masked if view["article_id"] == 2)["quote"] == "普通飞天并不好卖"


def test_one_view_is_not_attached_to_every_thesis():
    shared = "逆周期定价权让飞天在旺季也能提价，普通飞天动销也好"
    views = [
        _view(654, "甲", "positive", 1, judgment=shared, quote=shared),
        _view(2, "乙", "negative", 2, judgment="逆周期定价权并不稳", quote="逆周期定价权并不稳"),
        _view(3, "丙", "negative", 3, judgment="旺季提价失败了", quote="旺季提价失败了"),
        _view(4, "丁", "negative", 4, judgment="普通飞天动销放缓", quote="普通飞天动销放缓"),
    ]
    reply = (
        '{"theses": ['
        '{"title": "逆周期定价权", "mechanism": "淡季也能守住批价", "views": ["v654", "v2"]},'
        '{"title": "旺季提价", "mechanism": "旺季也能提价", "views": ["v654", "v3"]},'
        '{"title": "普通飞天动销", "mechanism": "终端动销", "views": ["v654", "v4"]}]}'
    )
    parsed = theses.parse_theses(reply, views, company="贵州茅台")
    cited = [ref for thesis in parsed for ref in (*thesis["for"], *thesis["against"], *thesis["mixed"])]
    assert cited.count(654) <= theses.MAX_PER_VIEW
    assert len(parsed) <= 2


def test_theses_refresh_at_most_daily_when_views_change():
    now = datetime(2026, 10, 20, tzinfo=UTC)
    row = {"views_hash": "old", "prompt_version": theses.PROMPT_VERSION, "created_at": now - timedelta(hours=3)}
    assert not theses.is_stale(row, "old", now=now)
    assert not theses.is_stale(row, "new", now=now)
    assert theses.is_stale({**row, "created_at": now - timedelta(days=2)}, "new", now=now)
    assert theses.is_stale(None, "new", now=now)
    first = [_view(1, "a", "", 1), _view(2, "b", "", 2)]
    assert theses.views_hash(first) == theses.views_hash(list(reversed(first)))
    assert theses.views_hash(first) != theses.views_hash([_view(1, "a", "", 1, judgment="改了")])


def test_theses_auto_disabled_by_default():
    from researchlib.config import Settings

    settings = Settings.from_env(
        {
            "DATABASE_URL": "postgresql://research:research@127.0.0.1:5432/research_test",
            "DATA_DIR": "/tmp/research-test-data",
            "PUBLIC_BASE_URL": "https://investment.zldong.com",
            "SOURCES_FILE": "config/sources.yaml",
            "AIHOT_MAP_FILE": "config/aihot_import.yaml",
            "BIND_HOST": "127.0.0.1",
            "BIND_PORT": "8088",
        }
    )
    assert settings.theses_auto_enabled is False
    assert Settings.from_env({**{
            "DATABASE_URL": "postgresql://research:research@127.0.0.1:5432/research_test",
            "DATA_DIR": "/tmp/research-test-data",
            "PUBLIC_BASE_URL": "https://investment.zldong.com",
            "SOURCES_FILE": "config/sources.yaml",
            "AIHOT_MAP_FILE": "config/aihot_import.yaml",
            "BIND_HOST": "127.0.0.1",
            "BIND_PORT": "8088",
            "RESEARCH_THESES_AUTO": "1",
        }}).theses_auto_enabled is True

