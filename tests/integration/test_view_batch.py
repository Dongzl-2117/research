"""research-views 一次补完积压：几篇同时整理、每篇占锁、记用量、余额不足整批停、避开高峰价。"""

import json
import os
import re
import threading
from datetime import UTC, datetime

from researchlib import views as views_mod
from researchlib.llm import LLMError
from researchlib.seed import load_sample
from researchlib.sources import load_sources


def _jobs(conn, settings):
    os.environ["ALLOW_SAMPLE_SEED"] = "1"
    load_sample(conn, settings)
    sources = [source for source in load_sources(settings.sources_path) if source.enabled]
    authors = {source.id: source.author for source in sources}
    wanted = [int(row["id"]) for row in views_mod.view_companies(conn, list(authors))]
    rows = views_mod.pending_articles(conn, wanted, list(authors), 50)
    jobs = [
        {"id": int(row["id"]), "title": row["title"], "source_id": row["source_id"], "companies": row["companies"]}
        for row in rows
    ]
    conn.commit()
    assert len(jobs) >= 3
    return jobs, authors, wanted


def _named(messages) -> list[str]:
    return re.search(r"指定的公司：(.*)", messages[1]["content"]).group(1).split("、")


def _no_view(_settings, messages, *, usage=None, **_kwargs):
    """每家公司都答「没下判断」，用量照接口那样加进调用方给的 dict。"""
    if usage is not None:
        for name, value in (("prompt", 1000), ("completion", 400), ("reasoning", 300)):
            usage[name] = usage.get(name, 0) + value
    return json.dumps({name: None for name in _named(messages)}, ensure_ascii=False), "deepseek-test"


def _pending(conn, wanted, authors) -> set[int]:
    conn.commit()
    return {int(row["id"]) for row in views_mod.pending_articles(conn, wanted, list(authors), 500)}


def test_several_workers_finish_the_queue_once_each_and_add_up_usage(conn, settings, monkeypatch, capsys):
    jobs, authors, wanted = _jobs(conn, settings)
    asked: list[str] = []
    guard = threading.Lock()

    def reply(_settings, messages, **kwargs):
        with guard:
            asked.append(re.search(r"标题：(.*)", messages[1]["content"]).group(1))
        return _no_view(_settings, messages, **kwargs)

    monkeypatch.setattr(views_mod, "chat", reply)
    report = views_mod.run_batch(settings, jobs, authors, workers=3)
    assert report["done"] == len(jobs) and report["failed"] == 0 and report["skipped"] == 0
    assert not report["fatal"] and not report["timed_out"]
    # 每篇只问一次模型，几个 worker 没有整理同一篇。
    assert len(asked) == len(jobs)
    assert report["usage"] == {"prompt": 1000 * len(jobs), "completion": 400 * len(jobs), "reasoning": 300 * len(jobs)}
    assert not _pending(conn, wanted, authors) & {job["id"] for job in jobs}
    lines = [line for line in capsys.readouterr().out.splitlines() if "家公司" in line]
    assert len(lines) == len(jobs)
    assert all("进 1,000 出 400（其中思考 300） token，用时" in line for line in lines)
    # 再跑一遍：已经整理过的不再问模型。
    again = views_mod.run_batch(settings, jobs, authors, workers=2)
    assert again["done"] == 0 and again["skipped"] == len(jobs) and len(asked) == len(jobs)


def test_article_locked_by_another_process_is_skipped(conn, settings, monkeypatch):
    jobs, authors, wanted = _jobs(conn, settings)
    busy = jobs[0]["id"]
    monkeypatch.setattr(views_mod, "chat", _no_view)
    conn.execute("SELECT pg_advisory_lock(hashtext(%s), %s)", (views_mod.LOCK_NAME, busy))
    conn.commit()
    try:
        report = views_mod.run_batch(settings, jobs, authors, workers=2)
    finally:
        conn.execute("SELECT pg_advisory_unlock(hashtext(%s), %s)", (views_mod.LOCK_NAME, busy))
        conn.commit()
    assert report["skipped"] == 1 and report["done"] == len(jobs) - 1
    assert _pending(conn, wanted, authors) & {job["id"] for job in jobs} == {busy}
    # 锁放掉以后，这一篇照常整理，整理完自己的锁也放掉了。
    assert views_mod.run_batch(settings, [jobs[0]], authors)["done"] == 1
    assert views_mod._claim(conn, busy)
    views_mod._release(conn, busy)


def test_out_of_balance_stops_the_batch_without_marking_articles_failed(conn, settings, monkeypatch):
    jobs, authors, wanted = _jobs(conn, settings)

    def broke(_settings, _messages, **_kwargs):
        raise LLMError("DeepSeek 接口返回 402：Insufficient Balance")

    monkeypatch.setattr(views_mod, "chat", broke)
    report = views_mod.run_batch(settings, jobs, authors, workers=2)
    assert "402" in report["fatal"] and report["done"] == 0 and report["failed"] == 0
    conn.commit()
    assert conn.execute("SELECT count(*) AS n FROM llm_failures WHERE kind = 'views'").fetchone()["n"] == 0
    # 没有记冷却：充了钱再跑，这几篇还在队列里。
    assert {job["id"] for job in jobs} <= _pending(conn, wanted, authors)


def test_one_bad_reply_is_recorded_and_the_rest_go_on(conn, settings, monkeypatch, capsys):
    jobs, authors, _wanted = _jobs(conn, settings)
    bad_title = jobs[1]["title"]

    def reply(_settings, messages, **kwargs):
        if f"标题：{bad_title}\n" in messages[1]["content"]:
            raise LLMError("DeepSeek 接口返回 500：upstream")
        return _no_view(_settings, messages, **kwargs)

    monkeypatch.setattr(views_mod, "chat", reply)
    report = views_mod.run_batch(settings, jobs, authors)
    assert report["failed"] == 1 and report["done"] == len(jobs) - 1 and not report["fatal"]
    conn.commit()
    failed = conn.execute("SELECT item_id FROM llm_failures WHERE kind = 'views'").fetchall()
    assert [row["item_id"] for row in failed] == [jobs[1]["id"]]
    assert "接口返回 500" in capsys.readouterr().err


def test_off_peak_waits_until_the_peak_price_window_ends(conn, settings, monkeypatch, capsys):
    jobs, authors, _wanted = _jobs(conn, settings)
    events: list[str] = []
    # 周三 UTC 7 点是高峰价（北京时间 15 点），10 点结束；等过两回以后到了 10:01。
    moments = iter([datetime(2026, 11, 11, 7, 0, tzinfo=UTC), datetime(2026, 11, 11, 9, 59, 30, tzinfo=UTC)])
    after = datetime(2026, 11, 11, 10, 1, tzinfo=UTC)

    def reply(_settings, messages, **kwargs):
        events.append("chat")
        return _no_view(_settings, messages, **kwargs)

    monkeypatch.setattr(views_mod, "chat", reply)
    report = views_mod.run_batch(
        settings,
        jobs[:2],
        authors,
        off_peak=True,
        now=lambda: next(moments, after),
        sleep=lambda seconds: events.append(f"sleep {seconds:.0f}"),
    )
    assert report["done"] == 2
    # 高峰里一篇都没开；离结束不到一分钟时只睡到结束。
    assert events == ["sleep 60", "sleep 30", "chat", "chat"]
    out = capsys.readouterr().out
    assert "北京时间 18:00 以后接着整理" in out and "过了 DeepSeek 高峰价时段，接着整理" in out


def test_time_limit_stops_opening_new_articles(conn, settings, monkeypatch):
    jobs, authors, _wanted = _jobs(conn, settings)
    monkeypatch.setattr(views_mod, "chat", _no_view)
    report = views_mod.run_batch(settings, jobs, authors, workers=2, deadline=0.0)
    assert report["timed_out"] and report["done"] == 0


def test_command_prints_totals_and_exits_2_when_the_batch_is_stopped(conn, settings, monkeypatch, capsys):
    jobs, _authors, _wanted = _jobs(conn, settings)
    monkeypatch.setattr(views_mod.Settings, "from_env", staticmethod(lambda: settings))
    monkeypatch.setattr(views_mod, "ensure_oauth", lambda _settings: None)

    def broke(_settings, _messages, **_kwargs):
        raise LLMError("DeepSeek 接口返回 402：Insufficient Balance")

    monkeypatch.setattr(views_mod, "chat", broke)
    assert views_mod.main(["--workers", "2", "--limit", "50"]) == 2
    captured = capsys.readouterr()
    assert captured.out.splitlines()[-1].startswith(f"待整理 {len(jobs)} 篇，整理 0 篇，失败 0 篇")
    assert "整批停下：DeepSeek 接口返回 402" in captured.err

    monkeypatch.setattr(views_mod, "chat", _no_view)
    assert views_mod.main(["--workers", "2", "--limit", "50"]) == 0
    total = capsys.readouterr().out.splitlines()[-1]
    assert total.startswith(
        f"待整理 {len(jobs)} 篇，整理 {len(jobs)} 篇，失败 0 篇 · 合计进 {1000 * len(jobs):,} 出 {400 * len(jobs):,}"
    )
