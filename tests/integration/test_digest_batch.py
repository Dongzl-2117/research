"""research-digest 一次补一大批：几篇同时写、每篇占锁、记用量、余额不足整批停。和 research-views 同一套跑法。"""

import os
import re
import threading

from researchlib import digest as digest_mod
from researchlib.llm import LLMError
from researchlib.seed import load_sample
from researchlib.sources import load_sources


def _jobs(conn, settings):
    os.environ["ALLOW_SAMPLE_SEED"] = "1"
    load_sample(conn, settings)
    sources = [source for source in load_sources(settings.sources_path) if source.enabled]
    authors = {source.id: source.author for source in sources}
    rows = digest_mod.pending_articles(conn, list(authors), 50)
    jobs = [{"id": int(row["id"]), "title": row["title"]} for row in rows]
    conn.commit()
    assert len(jobs) >= 3
    return jobs, authors


def _digest(_settings, _messages, *, usage=None, **_kwargs):
    """交回一份合格的摘要，用量照接口那样加进调用方给的 dict。"""
    if usage is not None:
        for name, value in (("prompt", 2000), ("completion", 500), ("reasoning", 350)):
            usage[name] = usage.get(name, 0) + value
    return '{"summary": "作者认为加载率还能升", "points": ["判断一", "判断二"], "tags": ["广告"]}', "deepseek-test"


def _pending(conn, authors) -> set[int]:
    conn.commit()
    return {int(row["id"]) for row in digest_mod.pending_articles(conn, list(authors), 500)}


def test_several_workers_write_each_digest_once_and_add_up_usage(conn, settings, monkeypatch, capsys):
    jobs, authors = _jobs(conn, settings)
    asked: list[str] = []
    guard = threading.Lock()

    def reply(_settings, messages, **kwargs):
        with guard:
            asked.append(re.search(r"标题：(.*)", messages[1]["content"]).group(1))
        return _digest(_settings, messages, **kwargs)

    monkeypatch.setattr(digest_mod, "chat", reply)
    report = digest_mod.run_batch(settings, jobs, authors, workers=3)
    assert report["done"] == len(jobs) and report["failed"] == 0 and report["skipped"] == 0
    assert not report["fatal"] and not report["timed_out"]
    # 每篇只问一次模型，几个 worker 没有写同一篇。
    assert len(asked) == len(jobs)
    assert report["usage"] == {"prompt": 2000 * len(jobs), "completion": 500 * len(jobs), "reasoning": 350 * len(jobs)}
    assert not _pending(conn, authors) & {job["id"] for job in jobs}
    lines = [line for line in capsys.readouterr().out.splitlines() if "要点 2 条" in line]
    assert len(lines) == len(jobs)
    assert all("进 2,000 出 500（其中思考 350） token，用时" in line for line in lines)
    # 再跑一遍：已经写过的不再问模型。
    again = digest_mod.run_batch(settings, jobs, authors, workers=2)
    assert again["done"] == 0 and again["skipped"] == len(jobs) and len(asked) == len(jobs)


def test_digest_locked_by_another_process_is_skipped(conn, settings, monkeypatch):
    jobs, authors = _jobs(conn, settings)
    busy = jobs[0]["id"]
    monkeypatch.setattr(digest_mod, "chat", _digest)
    conn.execute("SELECT pg_advisory_lock(hashtext(%s), %s)", (digest_mod.LOCK_NAME, busy))
    conn.commit()
    try:
        report = digest_mod.run_batch(settings, jobs, authors, workers=2)
    finally:
        conn.execute("SELECT pg_advisory_unlock(hashtext(%s), %s)", (digest_mod.LOCK_NAME, busy))
        conn.commit()
    assert report["skipped"] == 1 and report["done"] == len(jobs) - 1
    assert _pending(conn, authors) & {job["id"] for job in jobs} == {busy}
    # 锁放掉以后，这一篇照常写。
    assert digest_mod.run_batch(settings, [jobs[0]], authors)["done"] == 1


def test_out_of_balance_stops_the_batch_without_marking_articles_failed(conn, settings, monkeypatch):
    jobs, authors = _jobs(conn, settings)

    def broke(_settings, _messages, **_kwargs):
        raise LLMError("DeepSeek 接口返回 402：Insufficient Balance")

    monkeypatch.setattr(digest_mod, "chat", broke)
    report = digest_mod.run_batch(settings, jobs, authors, workers=2)
    assert "402" in report["fatal"] and report["done"] == 0 and report["failed"] == 0
    conn.commit()
    assert conn.execute("SELECT count(*) AS n FROM llm_failures WHERE kind = 'digest'").fetchone()["n"] == 0
    # 没有记冷却：充了钱再跑，这几篇还在队列里。
    assert {job["id"] for job in jobs} <= _pending(conn, authors)


def test_one_bad_reply_is_recorded_and_the_rest_go_on(conn, settings, monkeypatch, capsys):
    jobs, authors = _jobs(conn, settings)
    bad_title = jobs[1]["title"]

    def reply(_settings, messages, **kwargs):
        if f"标题：{bad_title}\n" in messages[1]["content"]:
            return "模型没有交回 JSON", "deepseek-test"
        return _digest(_settings, messages, **kwargs)

    monkeypatch.setattr(digest_mod, "chat", reply)
    report = digest_mod.run_batch(settings, jobs, authors)
    assert report["failed"] == 1 and report["done"] == len(jobs) - 1 and not report["fatal"]
    conn.commit()
    failed = conn.execute("SELECT item_id FROM llm_failures WHERE kind = 'digest'").fetchall()
    assert [row["item_id"] for row in failed] == [jobs[1]["id"]]
    assert "摘要返回里没有 JSON" in capsys.readouterr().err


def test_command_prints_totals_and_exits_2_when_the_batch_is_stopped(conn, settings, monkeypatch, capsys):
    jobs, _authors = _jobs(conn, settings)
    monkeypatch.setattr(digest_mod.Settings, "from_env", staticmethod(lambda: settings))
    monkeypatch.setattr(digest_mod, "ensure_oauth", lambda _settings: None)

    def broke(_settings, _messages, **_kwargs):
        raise LLMError("DeepSeek 接口返回 402：Insufficient Balance")

    monkeypatch.setattr(digest_mod, "chat", broke)
    assert digest_mod.main(["--workers", "2"]) == 2
    captured = capsys.readouterr()
    assert captured.out.splitlines()[-1].startswith(f"待处理 {len(jobs)} 篇，写入 0 篇，失败 0 篇")
    assert "整批停下：DeepSeek 接口返回 402" in captured.err

    monkeypatch.setattr(digest_mod, "chat", _digest)
    assert digest_mod.main(["--workers", "2"]) == 0
    total = capsys.readouterr().out.splitlines()[-1]
    assert total.startswith(
        f"待处理 {len(jobs)} 篇，写入 {len(jobs)} 篇，失败 0 篇 · 合计进 {2000 * len(jobs):,} 出 {500 * len(jobs):,}"
    )
