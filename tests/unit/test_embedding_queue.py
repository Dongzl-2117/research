"""成批算向量排队：一次只送一小份，线程之间、进程之间同一时刻只有一份在向量服务那里；检索的问题向量不排这个队。
不连数据库。"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import threading
import time

import pytest

from researchlib import embeddings


def _rows(article_id: int, count: int) -> list[dict]:
    return [
        {"article_id": article_id, "ord": i, "title": "标题", "heading": "", "body": f"第 {i} 段正文"}
        for i in range(count)
    ]


def _vector(text: str) -> list[float]:
    return [float(len(text))] + [0.0] * (embeddings.DIM - 1)


class _Service:
    """假的向量服务：记下每次送来几块、同一时刻有几份在算。"""

    def __init__(self, *, bad: str = "", down: bool = False) -> None:
        self.sizes: list[int] = []
        self.active = 0
        self.peak = 0
        self.bad = bad
        self.down = down
        self.lock = threading.Lock()

    def __call__(self, texts: list[str]) -> list[list[float]]:
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.sizes.append(len(texts))
        try:
            time.sleep(0.005)
            if self.down or (self.bad and any(self.bad in text for text in texts)):
                raise embeddings.EmbeddingError("算不出来")
            return [_vector(text) for text in texts]
        finally:
            with self.lock:
                self.active -= 1


@pytest.fixture
def service():
    fake = _Service()
    embeddings.use_embedder(fake)
    yield fake
    embeddings.use_embedder(None)


def test_bulk_vectors_go_a_few_at_a_time_and_one_part_at_a_time(service):
    # 译制 16 篇同时提交：以前每篇各送 32 块，问题向量排在几批后面。现在每次最多 BULK_BATCH 块，同一时刻只有一份。
    jobs = {article_id: _rows(article_id, 30) for article_id in range(1, 7)}
    results: dict[int, list] = {}

    def work(article_id: int) -> None:
        results[article_id] = embeddings._embed_rows(jobs[article_id], [])

    threads = [threading.Thread(target=work, args=(article_id,)) for article_id in jobs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert service.peak == 1
    assert max(service.sizes) <= embeddings.BULK_BATCH
    assert sum(service.sizes) == 6 * 30
    # 分份送不改结果：每块拿到的是自己那段字的向量，顺序和送进去的一样。
    for article_id, rows in jobs.items():
        pairs = results[article_id]
        assert [row for row, _ in pairs] == rows
        assert all(vector == _vector(embeddings._source_text(row)) for row, vector in pairs)


def test_question_vector_does_not_wait_behind_bulk_parts(service):
    # 别的线程或进程正占着成批的队：检索的问题向量照样直接去算。
    answer: list = []
    with embeddings._bulk_turn():
        thread = threading.Thread(target=lambda: answer.append(embeddings.query_vector("怎么判断定价权")))
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert answer == [_vector("怎么判断定价权")]


def test_bad_paragraph_is_skipped_and_a_dead_service_still_stops_the_job():
    # 分份以后照旧：服务好好的、只是某一块的字让它报错，跳过那一块；服务停了才往外报错。
    fake = _Service(bad="第 13 段")
    embeddings.use_embedder(fake)
    try:
        bad: list[tuple[int, int]] = []
        pairs = embeddings._embed_rows(_rows(5, 20), bad)
        assert [row["ord"] for row, _ in pairs] == [i for i in range(20) if i != 13]
        assert bad == [(5, 13)]
        embeddings.use_embedder(_Service(down=True))
        with pytest.raises(embeddings.EmbeddingError):
            embeddings._embed_rows(_rows(6, 20), [])
    finally:
        embeddings.use_embedder(None)


def test_bulk_queue_is_shared_with_other_processes():
    pytest.importorskip("fcntl")
    # 译制、识图、research-embed 各是一个进程：子进程拿着队睡一会儿，这边要等它放开才轮到。
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            textwrap.dedent(
                """
                import time
                from researchlib import embeddings
                with embeddings._bulk_turn():
                    print("locked", flush=True)
                    time.sleep(0.8)
                """
            ),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "locked"
        started = time.monotonic()
        with embeddings._bulk_turn():
            waited = time.monotonic() - started
    finally:
        child.wait(timeout=10)
    assert waited >= 0.4
