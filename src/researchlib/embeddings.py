"""语义检索：段落块的向量和按向量召回。

向量服务走 OpenAI 兼容的 ``POST {EMBEDDING_BASE_URL}/embeddings``。Mac mini 上的推荐做法是在 macOS 本机
（不在 Docker 里）跑 Ollama 的 bge-m3，用苹果芯片的 GPU；容器经 ``host.docker.internal`` 调它。
换成云端的向量 API 只改 ``.env``，不改代码。

向量存在 ``chunk_embeddings``（主键 article_id + ord），用 pgvector。不挂外键到 article_chunks：
重切块时块会先删再插，向量留着，靠 text_hash 判断这一块的字变没变，没变就不重算。
表要等数据库装了 pgvector 才建（``ensure_schema``，启动时和 research-embed 都会调），
没装、没配向量服务、服务连不上时，检索照旧只走关键词，不报错。

改段落块的地方（原文、译文、图片文字重切，清洗，导入）写完块以后在同一个事务里调 ``sync_article``：
新段落的向量和新段落一起提交，检索看到的要么是旧段落和旧向量，要么是新段落和新向量。
research-embed 只补服务连不上时漏下的。

    docker compose exec app research-embed            # 补算缺的和改过的块
    docker compose exec app research-embed --limit 2000
"""

from __future__ import annotations

import argparse
import contextvars
import hashlib
import http.client
import json
import logging
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from urllib.parse import urlsplit

import psycopg
from psycopg.rows import dict_row

from researchlib.chunks import current_chunk_sql

try:
    import fcntl
except ImportError:  # 不是 POSIX：成批算向量只在本进程里排队
    fcntl = None

logger = logging.getLogger("researchlib.embeddings")

# bge-m3 的维度。换模型时维度要一样，不一样就报错，不悄悄截断。
DIM = 1024
# 查询向量要快：连不上就当没有语义检索，这么多秒后再试。
QUERY_TIMEOUT_S = 4.0
RETRY_AFTER_S = 60.0
# 这么久没算过查询向量，本机 Ollama 多半已经把模型卸下了（默认闲置 5 分钟就卸），重新加载要几秒，
# 4 秒常常不够：以前隔一阵子后的第一次搜索总是超时，接下来一分钟的搜索也都只按字面找。
# 所以冷的时候放宽到 COLD_QUERY_TIMEOUT_S；刚失败过就不放宽，服务真卡住时每次搜索最多多等 4 秒。
WARM_FOR_S = 240.0
COLD_QUERY_TIMEOUT_S = 15.0
# 网站进程隔这么久没算过向量，就给本机 Ollama 算一个很短的向量（keep_warm），模型一直不卸下，
# 隔一阵子后的第一次搜索不用再等几秒加载。要比 Ollama 的 5 分钟短。
KEEP_WARM_S = 200.0
BATCH = 32
# 成批算向量（改段落时当场算、research-embed 补算）一次只送这么多块，而且全机同一时刻只有一批在向量服务那里
# （_bulk_turn）。检索的问题向量用同一个 Ollama，不排这个队，Ollama 按先来后到算，问题向量最多等手上这一小批。
# 以前一批 32 块、谁算完谁送：11-11 晚 16 篇同时译制，问题向量排在几批后面，线上新问题偶尔多等一到三秒；
# 等过 QUERY_TIMEOUT_S，检索就停一分钟只按字面找。
BULK_BATCH = 8
QUERY_CACHE = 256
# 检索页提示「向量还没算完」用的块数，几分钟数一次就够。
COVERAGE_TTL_S = 600.0
# 改段落时当场算向量（sync_article）。服务算不出来以后，这么多秒内别的文章不再试：
# 一批几千篇的任务不必每篇各等一次超时，缺的留给 research-embed。
SYNC_RETRY_AFTER_S = 60.0

Embedder = Callable[[list[str]], list[list[float]]]


class EmbeddingError(Exception):
    pass


class _State:
    def __init__(self) -> None:
        self.embedder: Embedder | None = None
        self.query_embedder: Embedder | None = None
        self.cold_embedder: Embedder | None = None
        self.model = ""
        self.down_until = 0.0
        # 改段落时算向量（sync_article）失败以后，到这个时刻之前不再试。和查询向量的 down_until 分开：
        # 批量任务算不出来，不该让网站的检索也停一分钟。
        self.sync_down_until = 0.0
        # 上一次查询向量成功的时刻，和上一次是不是失败了。
        self.last_ok = float("-inf")
        self.failed = False
        self.cache: OrderedDict[str, list[float]] = OrderedDict()
        # 正在后台算的问题向量（prefetch_query_vector），同一个问题不算两遍。
        self.inflight: dict[str, Future] = {}
        # 向量服务是不是本机的（Ollama）：只给本机的服务保温（keep_warm），云端 API 每次调用都花钱。
        self.local = False
        # 保温没成时只在日志里记第一次，Ollama 停着时不至于每分钟一行。
        self.warm_failing = False
        # 成批算向量的跨进程锁用不上时（锁文件打不开），日志只记一次。
        self.bulk_lock_warned = False
        self.schema: dict[str, str | None] = {}
        self.coverage: dict[str, tuple[tuple[int, int], float]] = {}
        # 正在后台重数块数的库（_refresh_coverage），一个库同时只数一遍。
        self.counting: set[str] = set()
        # use_embedder 换上的函数不被 configure 覆盖（测试里每开一条连接都会 configure 一次）。
        self.pinned = False
        self.lock = threading.Lock()


_state = _State()
# 后台算问题向量的线程（prefetch_query_vector）。问库时问题和几条要点一起算。
_PREFETCH = ThreadPoolExecutor(max_workers=4, thread_name_prefix="query-vector")


def _http_embedder(base_url: str, model: str, api_key: str, timeout: float) -> Embedder:
    def embed(texts: list[str]) -> list[list[float]]:
        body = json.dumps({"model": model, "input": texts}).encode()
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        request = urllib.request.Request(base_url + "/embeddings", data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = json.loads(response.read().decode())
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, http.client.HTTPException) as exc:
            raise EmbeddingError(f"向量服务 {base_url} 不可用：{exc}") from exc
        if not isinstance(data, dict):
            raise EmbeddingError("向量服务返回的条数或格式不对")
        items = sorted(data.get("data") or [], key=lambda item: item.get("index", 0))
        vectors = [item.get("embedding") for item in items]
        if len(vectors) != len(texts) or any(not isinstance(v, list) for v in vectors):
            raise EmbeddingError("向量服务返回的条数或格式不对")
        if any(len(v) != DIM for v in vectors):
            raise EmbeddingError(f"向量维度是 {len(vectors[0])}，这里要 {DIM}（bge-m3）。换模型要同维度。")
        return vectors

    return embed


def configure(settings) -> None:
    """按配置接上向量服务。没配 EMBEDDING_BASE_URL 就关掉语义检索。"""
    base = (getattr(settings, "embedding_base_url", "") or "").rstrip("/")
    model = getattr(settings, "embedding_model", "") or "bge-m3"
    with _state.lock:
        if _state.pinned:
            return
        if not base:
            _state.embedder = _state.query_embedder = _state.cold_embedder = None
            _state.model = ""
            return
        key = getattr(settings, "embedding_api_key", "") or ""
        timeout = float(getattr(settings, "embedding_timeout", 20.0) or 20.0)
        if _state.model == f"{base}|{model}" and _state.embedder is not None:
            return
        _state.embedder = _http_embedder(base, model, key, timeout)
        _state.query_embedder = _http_embedder(base, model, key, min(timeout, QUERY_TIMEOUT_S))
        _state.cold_embedder = _http_embedder(base, model, key, min(timeout, COLD_QUERY_TIMEOUT_S))
        _state.model = f"{base}|{model}"
        _state.local = _is_local(base)
        _reset()


def _is_local(base_url: str) -> bool:
    """本机或同一台机器上的服务（Ollama）。Mac mini 上容器经 host.docker.internal 调本机的 Ollama。"""
    try:
        parts = urlsplit(base_url)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return False
    return (
        host in {"localhost", "127.0.0.1", "::1", "host.docker.internal", "ollama"}
        or host.endswith(".local")
        or port == 11434
    )


def _reset() -> None:
    _state.cache.clear()
    _state.down_until = 0.0
    _state.sync_down_until = 0.0
    _state.last_ok = float("-inf")
    _state.failed = False
    _state.coverage.clear()
    _state.inflight.clear()
    _state.warm_failing = False


def use_embedder(embedder: Embedder | None, model: str = "test") -> None:
    """测试和脚本用：直接换一个算向量的函数。None 关掉语义检索。"""
    with _state.lock:
        _state.embedder = _state.query_embedder = _state.cold_embedder = embedder
        _state.pinned = embedder is not None
        _state.model = model if embedder else ""
        _state.local = False
        _reset()
        _state.schema.clear()


def model_name() -> str:
    return _state.model.rsplit("|", 1)[-1] if _state.model else ""


def enabled() -> bool:
    return _state.embedder is not None


def embed_texts(texts: list[str]) -> list[list[float]]:
    if _state.embedder is None:
        raise EmbeddingError("没有配置向量服务（EMBEDDING_BASE_URL）")
    return _state.embedder(texts)


# 成批算向量排的队（_bulk_turn）：本进程的线程先排 _BULK_THREADS，再和别的进程抢这个文件锁。网站、译制、识图、
# research-embed 都在同一个 app 容器里跑（docker compose exec），共用容器的临时目录。进程退出时锁自己放开。
_BULK_LOCK_PATH = os.path.join(tempfile.gettempdir(), "researchlib-embedding-bulk.lock")
_BULK_THREADS = threading.Lock()


@contextmanager
def _bulk_turn() -> Iterator[None]:
    """轮到这一份去算向量：同一台机器上同一时刻只有一份成批的在向量服务那里。问题向量（query_vector）不排这个队。

    锁文件打不开、加不上锁时只在本进程里排队，照常算，不因为排队的事让改段落的任务出错。
    """
    with _BULK_THREADS:
        fd = None
        if fcntl is not None:
            try:
                fd = os.open(_BULK_LOCK_PATH, os.O_RDONLY | os.O_CREAT, 0o644)
                fcntl.flock(fd, fcntl.LOCK_EX)
            except OSError as exc:
                if fd is not None:
                    os.close(fd)
                    fd = None
                if not _state.bulk_lock_warned:
                    _state.bulk_lock_warned = True
                    logger.warning("成批算向量只在本进程里排队（锁文件 %s 用不上：%s）", _BULK_LOCK_PATH, exc)
        try:
            yield
        finally:
            if fd is not None:
                os.close(fd)


def _bulk_embed(texts: list[str]) -> list[list[float]]:
    with _bulk_turn():
        return embed_texts(texts)


def prefetch_query_vector(text: str) -> None:
    """在后台先算问题的向量。检索一开始就调，向量服务那一趟和关键词召回的几条 SQL 同时进行；
    用到时 query_vector 等它算完，不再另算一次。结果和不预取时一样。"""
    text = (text or "").strip()[:2000]
    if not text or _state.query_embedder is None:
        return
    with _state.lock:
        if text in _state.cache or text in _state.inflight or time.monotonic() < _state.down_until:
            return
        future: Future = Future()
        _state.inflight[text] = future

    def run() -> None:
        try:
            future.set_result(_compute_query_vector(text))
        except BaseException as exc:  # 照原样交给等着的 query_vector 去报
            future.set_exception(exc)
        finally:
            with _state.lock:
                if _state.inflight.get(text) is future:
                    del _state.inflight[text]

    _PREFETCH.submit(run)


def query_vector(text: str) -> list[float] | None:
    """问题的向量。服务没配或刚失败过返回 None，调用方退回只用关键词。"""
    text = (text or "").strip()[:2000]
    if not text or _state.query_embedder is None:
        return None
    with _state.lock:
        cached = _state.cache.get(text)
        if cached is not None:
            _state.cache.move_to_end(text)
            return cached
        pending = _state.inflight.get(text)
    if pending is not None:
        return pending.result()
    return _compute_query_vector(text)


def _compute_query_vector(text: str) -> list[float] | None:
    with _state.lock:
        cached = _state.cache.get(text)
        if cached is not None:
            _state.cache.move_to_end(text)
            return cached
        now = time.monotonic()
        if now < _state.down_until:
            return None
        cold = not _state.failed and now - _state.last_ok > WARM_FOR_S
        embedder = _state.cold_embedder if cold else _state.query_embedder
        identity = (_state.model, _state.query_embedder, _state.cold_embedder)
    try:
        vector = embedder([text])[0]
    except EmbeddingError as exc:
        logger.warning("语义检索暂停 %s 秒：%s", int(RETRY_AFTER_S), exc)
        with _state.lock:
            if identity != (_state.model, _state.query_embedder, _state.cold_embedder):
                return None
            _state.down_until = time.monotonic() + RETRY_AFTER_S
            _state.failed = True
        return None
    with _state.lock:
        if identity != (_state.model, _state.query_embedder, _state.cold_embedder):
            return None
        _state.last_ok = time.monotonic()
        _state.failed = False
        _state.cache[text] = vector
        while len(_state.cache) > QUERY_CACHE:
            _state.cache.popitem(last=False)
    return vector


def paused() -> bool:
    """查询向量刚失败过，这一阵子检索只按字面找。"""
    return time.monotonic() < _state.down_until


def keep_warm() -> bool:
    """本机 Ollama 闲置 5 分钟就卸下模型，之后的第一次搜索要等它重新加载（几秒）。网站进程隔 KEEP_WARM_S
    没算过向量，就算一个很短的向量，让模型一直在。只给本机的服务做（云端 API 每次调用都花钱）。算了返回 True。

    用宽松超时（模型这时可能正在加载），失败也不暂停检索的语义这一路：只是保温没成，下一次搜索照常试。
    """
    with _state.lock:
        embedder = _state.cold_embedder
        if embedder is None or not _state.local or time.monotonic() - _state.last_ok < KEEP_WARM_S:
            return False
        identity = (_state.model, _state.query_embedder, _state.cold_embedder)
    try:
        embedder(["保温"])
    except EmbeddingError as exc:
        if not _state.warm_failing:
            logger.warning("向量服务保温没成（之后再失败不再记）：%s", exc)
        _state.warm_failing = True
        return False
    with _state.lock:
        if identity == (_state.model, _state.query_embedder, _state.cold_embedder):
            _state.last_ok = time.monotonic()
    if _state.warm_failing:
        logger.info("向量服务保温恢复")
    _state.warm_failing = False
    return True


def literal(vector: list[float]) -> str:
    return "[" + ",".join(f"{float(x):.6g}" for x in vector) + "]"


# —— 表 ——


def _vector_version(conn) -> tuple[int, ...] | None:
    row = conn.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'").fetchone()
    if row is None:
        return None
    return tuple(int(part) for part in str(row["extversion"]).split(".") if part.isdigit())


def ensure_schema(conn) -> bool:
    """装了 pgvector 就建向量表。没装返回 False，什么都不做。重复调用没有副作用。"""
    available = conn.execute("SELECT 1 FROM pg_available_extensions WHERE name = 'vector'").fetchone()
    if available is None:
        return False
    conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    version = _vector_version(conn) or (0,)
    # pgvector 0.7 起有半精度 halfvec，存储省一半；更老的用 vector。
    kind = "halfvec" if version >= (0, 7) else "vector"
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS chunk_embeddings (
          article_id bigint NOT NULL REFERENCES articles (id) ON DELETE CASCADE,
          ord integer NOT NULL,
          model text NOT NULL,
          text_hash text NOT NULL,
          embedding {kind}({DIM}) NOT NULL,
          updated_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (article_id, ord)
        )
        """
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS chunk_embeddings_hnsw_idx ON chunk_embeddings"
        f" USING hnsw (embedding {kind}_cosine_ops)"
    )
    conn.commit()
    _state.schema.clear()
    return True


def column_type(conn) -> str | None:
    """向量表的列类型（halfvec / vector）。表不存在返回 None。按连接的数据库缓存。"""
    key = conn.info.dbname
    if key in _state.schema:
        return _state.schema[key]
    row = conn.execute(
        """
        SELECT t.typname
        FROM pg_attribute att
        JOIN pg_type t ON t.oid = att.atttypid
        WHERE att.attrelid = to_regclass('chunk_embeddings') AND att.attname = 'embedding'
        """
    ).fetchone()
    kind = row["typname"] if row else None
    _state.schema[key] = kind
    return kind


def ready(conn) -> str | None:
    """能做语义检索时返回向量列类型，否则 None。"""
    if not enabled():
        return None
    return column_type(conn)


def tune_search(conn, pool: int = 200) -> None:
    """HNSW 默认只看 40 个邻居，要取 pool 块至少得看这么多（pgvector 上限 1000）。

    0.8 起有迭代扫描，不够时接着往下找。
    """
    version = _vector_version(conn) or (0,)
    conn.execute(f"SET LOCAL hnsw.ef_search = {max(200, min(1000, int(pool)))}")
    if version >= (0, 8):
        conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")


# —— 回填 ——

# 向量的输入是「标题 + 章节标题 + 块正文」：单看一块也知道它在谈哪家公司、哪一章。
_SOURCE_TEXT = "concat_ws(E'\\n', a.title, c.heading, c.body)"


def text_hash(title: str, heading: str, body: str) -> str:
    return hashlib.md5("\n".join([title or "", heading or "", body or ""]).encode()).hexdigest()


def _pending(
    conn,
    limit: int | None,
    model: str,
    skip: list[tuple[int, int]] | None = None,
    *,
    article_id: int | None = None,
    ords: tuple[int, int] | None = None,
) -> list[dict]:
    """缺向量或字改过的块，新的在前。skip 是这一轮算不出向量、跳过的块。

    article_id 给了只看这一篇，ords（含头不含尾）给了只看这一段块号；limit 为 None 不限。
    """
    skipped = ""
    params: list = [model]
    if skip:
        skipped = (
            "AND NOT EXISTS (SELECT 1 FROM unnest(%s::bigint[], %s::int[]) AS s(article_id, ord)"
            " WHERE s.article_id = c.article_id AND s.ord = c.ord)"
        )
        params += [[key[0] for key in skip], [key[1] for key in skip]]
    one = ""
    if article_id is not None:
        one = "AND c.article_id = %s"
        params.append(article_id)
    if ords is not None:
        one += " AND c.ord >= %s AND c.ord < %s"
        params += [ords[0], ords[1]]
    return conn.execute(
        f"""
        SELECT c.article_id, c.ord, a.title, c.heading, c.body, md5({_SOURCE_TEXT}) AS hash
        FROM article_chunks c
        JOIN articles a ON a.id = c.article_id
        LEFT JOIN chunk_embeddings e ON e.article_id = c.article_id AND e.ord = c.ord
        WHERE a.duplicate_of IS NULL AND {current_chunk_sql()}
          AND (e.article_id IS NULL OR e.text_hash <> md5({_SOURCE_TEXT}) OR e.model <> %s)
          {skipped} {one}
        ORDER BY a.published_at DESC NULLS LAST, c.article_id, c.ord
        LIMIT %s
        """,
        (*params, limit),
    ).fetchall()


def _reuse_vectors(
    conn, model: str, article_id: int | None = None, ords: tuple[int, int] | None = None,
) -> int:
    """缺向量或字改过的块，同一篇里已经有同样的字（标题、章节标题、正文都一样）的向量，就照抄过来，不重算。

    块号是按位置编的：前面删掉一块、或者整理规则只动了一两段，后面的块原样往前挪，旧向量还在、只是挂在别的块号上。
    要在删掉「没有对应块的向量」之前做，挪走的那几块的向量还在那些块号上。article_id 为 None 时整库做一遍；
    ords（含头不含尾）给了只补这一段块号的。
    """
    one = "AND c.article_id = %(article)s" if article_id is not None else ""
    if ords is not None:
        one += " AND c.ord >= %(lo)s AND c.ord < %(hi)s"
    cursor = conn.execute(
        f"""
        INSERT INTO chunk_embeddings (article_id, ord, model, text_hash, embedding)
        SELECT w.article_id, w.ord, %(model)s, w.hash, h.embedding
        FROM (
          SELECT c.article_id, c.ord, md5({_SOURCE_TEXT}) AS hash
          FROM article_chunks c
          JOIN articles a ON a.id = c.article_id
          LEFT JOIN chunk_embeddings e ON e.article_id = c.article_id AND e.ord = c.ord
          WHERE a.duplicate_of IS NULL AND {current_chunk_sql()} {one}
            AND (e.article_id IS NULL OR e.text_hash <> md5({_SOURCE_TEXT}) OR e.model <> %(model)s)
        ) w
        JOIN LATERAL (
          SELECT o.embedding FROM chunk_embeddings o
          WHERE o.article_id = w.article_id AND o.model = %(model)s AND o.text_hash = w.hash
          LIMIT 1
        ) h ON true
        ON CONFLICT (article_id, ord) DO UPDATE
        SET model = EXCLUDED.model, text_hash = EXCLUDED.text_hash, embedding = EXCLUDED.embedding, updated_at = now()
        """,
        {"model": model, "article": article_id, "lo": ords[0] if ords else None, "hi": ords[1] if ords else None},
    )
    return cursor.rowcount or 0


def _store_vectors(conn, kind: str, model: str, pairs: list[tuple[dict, list[float]]]) -> None:
    if not pairs:
        return
    with conn.cursor() as cur:
        cur.executemany(
            f"""
            INSERT INTO chunk_embeddings (article_id, ord, model, text_hash, embedding)
            VALUES (%s, %s, %s, %s, %s::{kind})
            ON CONFLICT (article_id, ord) DO UPDATE
            SET model = EXCLUDED.model, text_hash = EXCLUDED.text_hash,
                embedding = EXCLUDED.embedding, updated_at = now()
            """,
            [(r["article_id"], r["ord"], model, r["hash"], literal(v)) for r, v in pairs],
        )


def _source_text(row: dict) -> str:
    return "\n".join(part or '' for part in (row["title"], row["heading"], row["body"]))


def current_embedding_sql(embedding: str = 'e', chunk: str = 'c', article: str = 'a') -> str:
    return (f"{embedding}.model=%(embedding_model)s"
            f" AND {fresh_embedding_sql(embedding, chunk, article)}"
            f" AND {current_chunk_sql(chunk, article)}")


def fresh_embedding_sql(embedding: str = 'e', chunk: str = 'c', article: str = 'a') -> str:
    """向量是按这块现在的字算的。要把这块的字整个读出来算一遍 md5，几万块时是查询里最贵的一步。"""
    return f"{embedding}.text_hash=md5(concat_ws(E'\\n',{article}.title,{chunk}.heading,{chunk}.body))"


def _embed_rows(rows: list[dict], bad: list[tuple[int, int]]) -> list[tuple[dict, list[float]]]:
    """一批块的向量。按 BULK_BATCH 块一份送去算，每份排一次队（_bulk_turn），两份之间检索的问题向量插得进来。"""
    pairs: list[tuple[dict, list[float]]] = []
    for start in range(0, len(rows), BULK_BATCH):
        pairs.extend(_embed_part(rows[start : start + BULK_BATCH], bad))
    return pairs


def _embed_part(rows: list[dict], bad: list[tuple[int, int]]) -> list[tuple[dict, list[float]]]:
    """一份块的向量。整份报错时先试一下服务本身：服务正常，就是某几块的字让它报错，逐块算、坏的记进 bad
    跳过；服务也不正常（停了、连不上）才往外报错。

    以前整批报错就退出，下一轮还是从同一批开始（按时间排在最前），一块坏数据能让后面的块永远算不到。
    """
    texts = [_source_text(row) for row in rows]
    try:
        return list(zip(rows, _bulk_embed(texts), strict=True))
    except EmbeddingError as exc:
        error = exc
    try:
        _bulk_embed(["向量服务检查"])
    except EmbeddingError:
        raise error from None
    pairs: list[tuple[dict, list[float]]] = []
    for row, text in zip(rows, texts, strict=True):
        try:
            pairs.append((row, _bulk_embed([text])[0]))
        except EmbeddingError as exc:
            logger.warning("第 %s 篇第 %s 块算不出向量，跳过：%s", row["article_id"], row["ord"], exc)
            bad.append((row["article_id"], row["ord"]))
    return pairs


def backfill(
    conn,
    *,
    limit: int | None = None,
    batch: int = BATCH,
    progress: Callable[[int], None] | None = None,
    skipped: list[tuple[int, int]] | None = None,
) -> int:
    """给缺向量、或字改过的块算向量。删掉已经没有对应块的向量。返回这次算了多少块。

    算不出向量的块跳过，(文章, 块号) 记进 skipped，接着算后面的；服务停了才报错。
    """
    kind = column_type(conn)
    if kind is None:
        raise EmbeddingError("数据库里没有 chunk_embeddings：先给 Postgres 装 pgvector（重建 db 镜像）")
    model = model_name() or "unknown"
    _reuse_vectors(conn, model)
    conn.execute(
        """
        DELETE FROM chunk_embeddings e
        WHERE NOT EXISTS (SELECT 1 FROM article_chunks c WHERE c.article_id = e.article_id AND c.ord = e.ord)
        """
    )
    conn.commit()
    done = 0
    bad: list[tuple[int, int]] = []
    while limit is None or done < limit:
        size = batch if limit is None else min(batch, limit - done)
        rows = _pending(conn, size, model, bad)
        if not rows:
            break
        pairs = _embed_rows(rows, bad)
        if pairs:
            _store_vectors(conn, kind, model, pairs)
            conn.commit()
        done += len(pairs)
        if progress:
            progress(done)
    if skipped is not None:
        skipped.extend(bad)
    _state.coverage.clear()
    return done


# 清洗、重切这类随时能重跑的命令在 all_or_nothing() 里跑：向量算不出来就往外报错，不留下有字没向量的段落。
_ALL_OR_NOTHING: contextvars.ContextVar[bool] = contextvars.ContextVar("embeddings_all_or_nothing", default=False)


@contextmanager
def all_or_nothing() -> Iterator[None]:
    """这里面改段落时，向量服务算不出来就往外报 EmbeddingError，调用方回滚这一批、停下。

    给 research-clean、research-rebuild-chunks 用：它们只按现有的字重算，不花模型钱，服务好了再跑一遍，
    从还没改的那篇接着来。译制、识图、导入不包这一层：结果花过模型钱，照常存下，缺的向量留给 research-embed。
    只管当前线程（ContextVar），网站后台线程里的重切不受影响。
    """
    token = _ALL_OR_NOTHING.set(True)
    try:
        yield
    finally:
        _ALL_OR_NOTHING.reset(token)


def sync_article(conn, article_id: int, ords: tuple[int, int] | None = None) -> int:
    """这篇的段落刚改过（重切、清洗、换了译文或图片文字、改了标题），在提交之前调：给它的段落配齐向量。

    以前改段落和算向量是两步：段落先换上，向量等 research-embed（夜里、或者手动跑）再算。中间这段时间，
    改过的段落只能按字面找到，融合分只剩关键词那一半，整批往下掉：11-11 research-clean 改了约 6000 篇以后，
    检索页报「向量还没算完（219443/236724 块）」；全库重译时每译完一篇，它的中文段落也要等到夜里。

    现在：字没变的块（块号挪了也认）照抄这篇已有的向量；其余的当场算；没有对应块的向量删掉。都在调用方的事务里，
    别的连接在提交之前看到的是旧段落和旧向量，提交以后是新段落和新向量，中间没有有字没向量的段落。
    返回当场算的块数。

    ords（含头不含尾）：只管这一段块号。原文、译文、图片文字的块号各占一段（0 起、10000 起、20000 起，见 chunks）。
    只重切了译文块就只配译文块的向量：清洗原文和重译同一篇的两个任务同时跑时，各改各的向量行，不互相等锁。
    标题改了（向量的输入带标题）不给 ords，整篇一起配。

    没配向量服务、库里没有向量表时什么都不做。服务算不出来：在 all_or_nothing() 里往外报错；不在的话记一条日志，
    没算的留给 research-embed，SYNC_RETRY_AFTER_S 秒内别的文章也不再试。服务好好的、只是某一块的字让它报错的，
    跳过那一块（research-embed 也会跳过它），不算算不出来。
    """
    if _state.embedder is None:
        return 0
    kind = column_type(conn)
    if kind is None:
        return 0
    model = model_name() or "unknown"
    _reuse_vectors(conn, model, article_id, ords)
    lo, hi = ords if ords is not None else (None, None)
    conn.execute(
        "DELETE FROM chunk_embeddings e WHERE e.article_id = %(article)s"
        " AND (%(lo)s::int IS NULL OR (e.ord >= %(lo)s AND e.ord < %(hi)s))"
        " AND NOT EXISTS (SELECT 1 FROM article_chunks c WHERE c.article_id = e.article_id AND c.ord = e.ord)",
        {"article": article_id, "lo": lo, "hi": hi},
    )
    rows = _pending(conn, None, model, article_id=article_id, ords=ords)
    if not rows:
        return 0
    strict = _ALL_OR_NOTHING.get()
    if not strict and time.monotonic() < _state.sync_down_until:
        return 0
    done = 0
    bad: list[tuple[int, int]] = []
    try:
        for start in range(0, len(rows), BATCH):
            pairs = _embed_rows(rows[start : start + BATCH], bad)
            _store_vectors(conn, kind, model, pairs)
            done += len(pairs)
    except EmbeddingError as exc:
        if strict:
            raise
        _state.sync_down_until = time.monotonic() + SYNC_RETRY_AFTER_S
        logger.warning("第 %s 篇还有 %s 块没算上向量，留给 research-embed：%s", article_id, len(rows) - done, exc)
    return done


def coverage(conn) -> tuple[int, int]:
    """(有向量的块, 全部块)。"""
    if column_type(conn) is None:
        return 0, 0
    row = conn.execute(
        f"""
        SELECT count(e.article_id) AS done,count(*) AS total
        FROM article_chunks c JOIN articles a ON a.id=c.article_id
        LEFT JOIN chunk_embeddings e ON e.article_id=c.article_id AND e.ord=c.ord
          AND {current_embedding_sql()}
        WHERE a.duplicate_of IS NULL AND {current_chunk_sql()}
        """, {'embedding_model': model_name()},
    ).fetchone()
    return int(row["done"]), int(row["total"])


def _cached_coverage(conn) -> tuple[int, int]:
    """检索页那句提示用的块数。数一遍要把全库每块的字过一遍 md5（二十多万块要一两秒），以前过期了就在
    这次搜索里当场重数：隔十分钟以上才搜一次的话，次次都要多等这一两秒。现在过期了先用上次的数，
    另开一条连接在后台重数；只有这个进程第一次用（还没有数）时才当场数。"""
    key = conn.info.dbname
    now = time.monotonic()
    hit = _state.coverage.get(key)
    if hit is None:
        value = coverage(conn)
        _state.coverage[key] = (value, now)
        return value
    if now - hit[1] >= COVERAGE_TTL_S:
        _refresh_coverage(conn, key, hit)
    return hit[0]


def _refresh_coverage(conn, key: str, stale: tuple[tuple[int, int], float]) -> None:
    with _state.lock:
        if _state.coverage.get(key) is not stale or key in _state.counting:
            return
        _state.counting.add(key)
    dsn, password = conn.info.dsn, conn.info.password

    def run() -> None:
        try:
            with psycopg.connect(dsn, password=password, row_factory=dict_row) as other:
                value = coverage(other)
            with _state.lock:
                if _state.coverage.get(key) is stale:
                    _state.coverage[key] = (value, time.monotonic())
        except Exception as exc:  # 数不出来就下次再数，不影响检索
            logger.info("后台重数向量块数没成：%s", exc)
        finally:
            with _state.lock:
                _state.counting.discard(key)

    threading.Thread(target=run, name="embedding-coverage", daemon=True).start()


def status_note(conn) -> str:
    """检索页上的一句话：语义检索为什么没用上，或者向量还没算完。一切正常时是空字符串。

    语义检索出了问题以前只在日志里，页面上看不出来，搜「costco 商业模式」找不到按意思相关的文章时，
    分不清是没配、没算向量、服务连不上，还是真的没有。
    """
    if not enabled():
        return "没开语义检索（没配 EMBEDDING_BASE_URL），这次只按字面找。"
    if column_type(conn) is None:
        return "数据库没装 pgvector，语义检索没开，这次只按字面找。"
    if paused():
        return "向量服务连不上，这次只按字面找，过一分钟再试。"
    have, total = _cached_coverage(conn)
    if total and have == 0:
        return "向量还一块都没算（research-embed），这次只按字面找。"
    if total and have < total * 0.98:
        return f"向量还没算完（{have}/{total} 块），没算到的段落只能按字面找到。"
    return ""


def main(argv: list[str] | None = None) -> int:
    from researchlib.config import Settings
    from researchlib.db import prepare

    parser = argparse.ArgumentParser(description="给段落块补算语义检索用的向量")
    parser.add_argument("--limit", type=int, default=None, help="这次最多算多少块（默认全部）")
    parser.add_argument("--batch", type=int, default=BATCH)
    args = parser.parse_args(argv)
    settings = Settings.from_env()
    if not settings.embedding_base_url:
        print("没有配置 EMBEDDING_BASE_URL，跳过。见 docs/retrieval.md「语义检索」。")
        return 0
    conn = prepare(settings)
    try:
        if not ensure_schema(conn):
            print("Postgres 没有 pgvector：重建 db 镜像（docker compose up -d --build db）。", file=sys.stderr)
            return 1
        started = time.monotonic()

        step = max(1, args.batch) * 20
        reported = [0]

        def report(done: int) -> None:
            if done // step > reported[0] // step:
                print(f"已算 {done} 块，{time.monotonic() - started:.0f} 秒", flush=True)
            reported[0] = done

        skipped: list[tuple[int, int]] = []
        count = backfill(conn, limit=args.limit, batch=max(1, args.batch), progress=report, skipped=skipped)
        have, total = coverage(conn)
    except EmbeddingError as exc:
        print(f"向量回填失败：{exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(f"这次算了 {count} 块，库里 {have}/{total} 块有向量，用时 {time.monotonic() - started:.0f} 秒")
    if skipped:
        shown = "、".join(f"{article_id}#{ord_}" for article_id, ord_ in skipped[:10])
        more = f" 等 {len(skipped)} 块" if len(skipped) > 10 else ""
        print(f"{len(skipped)} 块算不出向量，跳过了：{shown}{more}（文章 id#块号）", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
