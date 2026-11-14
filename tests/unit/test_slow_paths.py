"""审核组 4「慢」：问库轮询的轻量版、对照页的进程内缓存。"""

import threading

from researchlib.web.api import brief_payload
from researchlib.web.render_cache import RenderCache


def _citations(count):
    return [
        {"n": n, "url": f"/articles/{n}", "title": f"文章{n}", "text": "原文" * 200,
         "before": "前文" * 200, "after": "后文" * 200, "article_id": n}
        for n in range(1, count + 1)
    ]


def test_brief_answer_while_running_only_carries_cited_passages():
    payload = {"id": 7, "status": "running", "answer": "先说结论[3]，再看[12]和[3]。", "citations": _citations(240)}
    brief = brief_payload(payload)
    assert [item["n"] for item in brief["citations"]] == [3, 12]
    assert all("before" not in item and "after" not in item for item in brief["citations"])
    assert brief["citations"][0]["text"] and brief["citations"][0]["url"] == "/articles/3"
    assert brief["answer"] == payload["answer"] and len(payload["citations"]) == 240  # 原样不动
    assert brief_payload({**payload, "answer": ""})["citations"] == []


def test_brief_answer_when_done_carries_every_passage_without_context():
    payload = {"id": 7, "status": "done", "answer": "结论[1]。", "citations": _citations(5)}
    brief = brief_payload(payload)
    assert [item["n"] for item in brief["citations"]] == [1, 2, 3, 4, 5]
    assert all(item["text"] and "before" not in item for item in brief["citations"])


def test_render_cache_reuses_results_for_the_same_input_only():
    cache = RenderCache(max_entries=2)
    calls = []

    def work(text, *, extra=None):
        calls.append((text, extra))
        return text.upper()

    assert cache.call("w", work, "abc", extra={"k": 1}) == "ABC"
    assert cache.call("w", work, "abc", extra={"k": 1}) == "ABC"
    assert cache.call("w", work, "abc", extra={"k": 2}) == "ABC"  # 回执变了要重算
    assert cache.call("other", work, "abc", extra={"k": 1}) == "ABC"  # 名字不同不共用
    assert len(calls) == 3 and cache.hits == 1
    cache.call("w", work, "abc", extra={"k": 1})  # 已经被挤出去了（只留两条）
    assert len(calls) == 4


def test_render_cache_is_safe_across_threads():
    cache = RenderCache(max_entries=8)
    errors = []

    def run(seed):
        try:
            for i in range(200):
                key = f"{(seed + i) % 12}"
                assert cache.call("t", lambda v: v * 2, key) == key * 2
        except Exception as exc:  # pragma: no cover - 失败时报出来
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
