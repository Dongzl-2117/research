"""出问题才提示：状态文件 → 首页提示；Grok 连续失败计数；断更提示只看定时导入的来源。"""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from researchlib import health
from researchlib.web.present import stale_sources

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def _write(data_dir, task, **fields):
    path = data_dir / "status" / f"{task}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"task": task, **fields}, ensure_ascii=False), encoding="utf-8")


def test_nothing_to_say_when_everything_is_fine(tmp_path):
    assert health.notices(tmp_path, "Asia/Shanghai", now=NOW) == []
    _write(tmp_path, "backup", ok=True, at="2026-10-20T03:17:00Z", message="7.5M，6000 篇")
    _write(tmp_path, "restore-check", ok=True, at="2026-10-01T04:23:00Z")
    _write(tmp_path, "upkeep", ok=True, at="2026-09-01T00:00:00Z")
    assert health.notices(tmp_path, "Asia/Shanghai", now=NOW) == []


def test_failures_and_overdue_tasks_are_reported(tmp_path):
    _write(tmp_path, "backup", ok=False, at="2026-10-20T03:17:00Z", message="导出数据库失败（退出码 1）：连不上")
    _write(tmp_path, "restore-check", ok=True, at="2026-08-01T04:23:00Z")
    _write(tmp_path, "nightly", ok=True, at="2026-10-17T16:37:00Z")
    found = health.notices(tmp_path, "Asia/Shanghai", now=NOW)
    assert found[0] == "备份失败（10-20 11:17）：导出数据库失败（退出码 1）：连不上"
    assert any(text.startswith("试恢复已经 80 天没成功了") for text in found)
    assert any(text.startswith("夜间补齐已经 2 天没成功了") for text in found)


def test_status_file_with_broken_bytes_still_counts(tmp_path):
    path = tmp_path / "status" / "backup.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(b'{"task": "backup", "ok": false, "at": "2026-10-20T03:17:00Z", "message": "\xe5\xa4"}')
    assert health.notices(tmp_path, "Asia/Shanghai", now=NOW)[0].startswith("备份失败")


def test_llm_failures_are_counted_until_a_success(tmp_path):
    health.reset_llm_cache()
    health.record_llm(tmp_path, ok=True, provider="Grok")
    assert not (tmp_path / "status" / "llm.json").exists()
    for _ in range(3):
        health.record_llm(tmp_path, ok=False, error="Grok 接口返回 503：busy", provider="Grok")
    state = health.read_status(tmp_path, "llm")
    assert state["ok"] is False and state["failures"] == 3 and state["provider"] == "Grok"
    assert any("Grok 调用连续失败 3 次" in text for text in health.notices(tmp_path, "Asia/Shanghai"))
    health.record_llm(tmp_path, ok=True, provider="Grok")
    assert health.read_status(tmp_path, "llm")["ok"] is True
    assert health.notices(tmp_path, "Asia/Shanghai") == []
    health.record_llm(tmp_path, ok=False, error="没有 xAI 凭证", unavailable=True, provider="Grok")
    assert any("Grok 登录失效" in text for text in health.notices(tmp_path, "Asia/Shanghai"))
    health.reset_llm_cache()


def test_llm_notice_names_deepseek(tmp_path):
    health.reset_llm_cache()
    for _ in range(3):
        health.record_llm(tmp_path, ok=False, error="DeepSeek 接口返回 503：busy", provider="DeepSeek")
    assert any("DeepSeek 调用连续失败 3 次" in text for text in health.notices(tmp_path, "Asia/Shanghai"))
    health.reset_llm_cache()
    health.record_llm(tmp_path, ok=False, error="没有 DEEPSEEK_API_KEY", unavailable=True, provider="DeepSeek")
    found = health.notices(tmp_path, "Asia/Shanghai")
    assert any("DeepSeek 凭证不可用" in text and "DEEPSEEK_API_KEY" in text for text in found)
    health.reset_llm_cache()


def test_stale_banner_only_watches_scheduled_imports():
    every_three_days = [NOW - timedelta(days=20 + 3 * index) for index in range(15)]
    daily = [NOW - timedelta(days=1 + index) for index in range(15)]
    sources = [SimpleNamespace(id="a", name="甲公众号"), SimpleNamespace(id="b", name="乙星球")]
    fresh = NOW - timedelta(hours=5)
    times = {"a": every_three_days, "b": daily}
    # 定时导入在跑、只有甲停了：提示甲。
    found = stale_sources(
        sources, times, {"a": {"at": fresh, "origin": "aihot"}, "b": {"at": fresh, "origin": "aihot"}},
        "Asia/Shanghai", now=NOW,
    )
    assert [item["name"] for item in found] == ["甲公众号"]
    assert found[0]["idle_days"] == 20 and "3 天左右一篇" in found[0]["typical"] and found[0]["imported"]
    # 只手动导过归档、或者定时导入几天没跑了：不提示。
    assert stale_sources(sources, times, {"a": {"at": fresh, "origin": "archive"}}, "Asia/Shanghai", now=NOW) == []
    old = NOW - timedelta(days=5)
    assert stale_sources(sources, times, {"a": {"at": old, "origin": "aihot"}}, "Asia/Shanghai", now=NOW) == []
    # 定时导入的来源全都断更：多半是 AIHOT 抓取停了，只说一次。
    both = {"a": every_three_days, "b": every_three_days}
    imports = {"a": {"at": fresh, "origin": "aihot"}, "b": {"at": fresh, "origin": "aihot"}}
    assert stale_sources(sources, both, imports, "Asia/Shanghai", now=NOW) == [
        {"all": True, "count": 2, "idle_days": 20}
    ]
    assert stale_sources(sources, {"a": every_three_days[:5]}, imports, "Asia/Shanghai", now=NOW) == []


def test_crawl_tasks_are_watched(tmp_path):
    _write(tmp_path, "crawl-fangwei-mp", ok=False, at="2026-10-20T00:10:00Z", message="缺少 DAJIALA_KEY")
    _write(tmp_path, "crawl-fangwei-zsxq", ok=True, at="2026-10-18T12:40:00Z")
    found = health.notices(tmp_path, "Asia/Shanghai", now=NOW)
    assert "方伟公众号抓取失败（10-20 08:10）：缺少 DAJIALA_KEY" in found
    assert any(text.startswith("方伟星球抓取已经 47 小时没成功了") for text in found)


def test_stale_banner_watches_crawl_imports():
    """AIHOT 下线以后，定时抓取脚本导入的来源（origin=crawl）也要看。"""
    every_three_days = [NOW - timedelta(days=20 + 3 * index) for index in range(15)]
    sources = [SimpleNamespace(id="a", name="甲公众号")]
    fresh = NOW - timedelta(hours=5)
    found = stale_sources(
        sources, {"a": every_three_days}, {"a": {"at": fresh, "origin": "crawl"}}, "Asia/Shanghai", now=NOW
    )
    assert [item["name"] for item in found] == ["甲公众号"]


def test_ingest_cli_records_crawl_origin(monkeypatch, tmp_path):
    from researchlib.ingest import cli

    seen = {}
    monkeypatch.setattr(cli.Settings, "from_env", classmethod(lambda klass: SimpleNamespace(
        sources_path=tmp_path / "sources.yaml", data_dir=tmp_path)))
    monkeypatch.setattr(cli, "load_sources", lambda path: [])
    monkeypatch.setattr(cli, "require_source", lambda sources, source_id, kind: SimpleNamespace(id=source_id))
    monkeypatch.setattr(cli, "prepare", lambda settings: SimpleNamespace(close=lambda: None, rollback=lambda: None))
    monkeypatch.setattr(cli, "parse_archive", lambda *args, **kwargs: ([], [], 0, 0))

    def fake_store(conn, **kwargs):
        seen["origin"] = kwargs["origin"]
        return SimpleNamespace(errors=[], render=lambda: "")

    monkeypatch.setattr(cli, "store_documents", fake_store)
    argv = ["--source", "fangwei-mp", "--input", str(tmp_path)]
    assert cli.run_markdown_ingest("wechat_mp", "测试", argv + ["--origin", "crawl"], id_mode="path") == 0
    assert seen["origin"] == "crawl"
    assert cli.run_markdown_ingest("wechat_mp", "测试", argv, id_mode="path") == 0
    assert seen["origin"] == "archive"


def test_deploy_failures_are_reported_but_quiet_weeks_are_not(tmp_path):
    """自动上线只在有新提交时才跑一次，很久没上线不算问题；失败了要提示。"""
    _write(tmp_path, "deploy", ok=True, at="2026-09-01T00:00:00Z", message="上线 abc1234：改标题")
    assert health.notices(tmp_path, "Asia/Shanghai", now=NOW) == []
    _write(tmp_path, "deploy", ok=False, at="2026-10-20T02:00:00Z", message="main 上的 abc1234 CI 没通过，没有上线")
    assert health.notices(tmp_path, "Asia/Shanghai", now=NOW) == [
        "自动上线失败（10-20 10:00）：main 上的 abc1234 CI 没通过，没有上线"
    ]
