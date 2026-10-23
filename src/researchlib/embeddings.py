"""语义检索：段落块的向量和按向量召回。

向量服务走 OpenAI 兼容的 ``POST {EMBEDDING_BASE_URL}/embeddings``。Mac mini 上的推荐做法是在 macOS 本机
（不在 Docker 里）跑 Ollama 的 bge-m3，用苹果芯片的 GPU；容器经 ``host.docker.internal`` 调它。
换成云端的向量 API 只改 ``.env``，不改代码。

向量存在 ``chunk_embeddings``（主键 article_id + ord），用 pgvector。不挂外键到 article_chunks：
重切块时块会先删再插，向量留着，靠 text_hash 判断这一块的字变没变，没变就不重算。
表要等数据库装了 pgvector 才建（``ensure_schema``，启动时和 research-embed 都会调），
没装、没配向量服务、服务连不上时，检索照旧只走关键词，不报错。

    docker compose exec app research-embed            # 补算缺的和改过的块
    docker compose exec app research-embed --limit 2000
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from collections.abc import Callable

from researchlib.eligibility import (
    chunk_provenance_article,
    chunk_provenance_sql,
    model_cache_safe_article,
    model_cache_safe_sql,
)
from researchlib.empty_answer import is_suppressed, visible_sql

logger = logging.getLogger("researchlib.embeddings")

# bge-m3 的维度。换模型时维度要一样，不一样就报错，不悄悄截断。
DIM = 1024
# 查询向量要快：连不上就当没有语义检索，这么多秒后再试。
QUERY_TIMEOUT_S = 4.0
RETRY_AFTER_S = 60.0
BATCH = 32
QUERY_CACHE = 256

Embedder = Callable[[list[str]], list[list[float]]]


class EmbeddingError(Exception):
    pass


class _State:
    def __init__(self) -> None:
        self.embedder: Embedder | None = None
        self.query_embedder: Embedder | None = None
        self.model = ""
        self.down_until = 0.0
        self.cache: OrderedDict[str, list[float]] = OrderedDict()
        self.schema: dict[str, str | None] = {}
        # use_embedder 换上的函数不被 configure 覆盖（测试里每开一条连接都会 configure 一次）。
        self.pinned = False
        self.lock = threading.Lock()


_state = _State()


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
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise EmbeddingError(f"向量服务 {base_url} 不可用：{exc}") from exc
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
            _state.embedder = _state.query_embedder = None
            _state.model = ""
            return
        key = getattr(settings, "embedding_api_key", "") or ""
        timeout = float(getattr(settings, "embedding_timeout", 20.0) or 20.0)
        if _state.model == f"{base}|{model}" and _state.embedder is not None:
            return
        _state.embedder = _http_embedder(base, model, key, timeout)
        _state.query_embedder = _http_embedder(base, model, key, min(timeout, QUERY_TIMEOUT_S))
        _state.model = f"{base}|{model}"
        _state.cache.clear()
        _state.down_until = 0.0


def use_embedder(embedder: Embedder | None, model: str = "test") -> None:
    """测试和脚本用：直接换一个算向量的函数。None 关掉语义检索。"""
    with _state.lock:
        _state.embedder = _state.query_embedder = embedder
        _state.pinned = embedder is not None
        _state.model = model if embedder else ""
        _state.cache.clear()
        _state.down_until = 0.0
        _state.schema.clear()


def model_name() -> str:
    return _state.model.rsplit("|", 1)[-1] if _state.model else ""


def enabled() -> bool:
    return _state.embedder is not None


def embed_texts(texts: list[str]) -> list[list[float]]:
    if _state.embedder is None:
        raise EmbeddingError("没有配置向量服务（EMBEDDING_BASE_URL）")
    return _state.embedder(texts)


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
        if time.monotonic() < _state.down_until:
            return None
        embedder = _state.query_embedder
    try:
        vector = embedder([text])[0]
    except EmbeddingError as exc:
        logger.warning("语义检索暂停 %s 秒：%s", int(RETRY_AFTER_S), exc)
        with _state.lock:
            _state.down_until = time.monotonic() + RETRY_AFTER_S
        return None
    with _state.lock:
        _state.cache[text] = vector
        while len(_state.cache) > QUERY_CACHE:
            _state.cache.popitem(last=False)
    return vector


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


def tune_search(conn) -> None:
    """HNSW 默认只看 40 个邻居，带范围过滤时不够。0.8 起有迭代扫描，过滤掉的会接着往下找。"""
    version = _vector_version(conn) or (0,)
    conn.execute("SET LOCAL hnsw.ef_search = 200")
    if version >= (0, 8):
        conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")


# —— 回填 ——

# 向量的输入是「标题 + 章节标题 + 块正文」：单看一块也知道它在谈哪家公司、哪一章。
_SOURCE_TEXT = "concat_ws(E'\\n', a.title, c.heading, c.body)"


def text_hash(title: str, heading: str, body: str) -> str:
    return hashlib.md5("\n".join([title or "", heading or "", body or ""]).encode()).hexdigest()


def _pending(conn, limit: int, model: str) -> list[dict]:
    return conn.execute(
        f"""
        SELECT c.article_id, c.ord, a.title, c.heading, c.body, md5({_SOURCE_TEXT}) AS hash
        FROM article_chunks c
        JOIN articles a ON a.id = c.article_id
        LEFT JOIN chunk_embeddings e ON e.article_id = c.article_id AND e.ord = c.ord
        WHERE a.duplicate_of IS NULL
          AND {visible_sql("a")}
          AND {model_cache_safe_sql("a")}
          AND {chunk_provenance_sql("a", "c")}
          AND (e.article_id IS NULL OR e.text_hash <> md5({_SOURCE_TEXT}) OR e.model <> %s)
        ORDER BY a.published_at DESC NULLS LAST, c.article_id, c.ord
        LIMIT %s
        """,
        (model, limit),
    ).fetchall()


def _current_rows(conn, candidates: list[dict]) -> list[dict]:
    """Re-read proof immediately before an external embedding request."""
    if not candidates:
        return []
    rows = conn.execute(
        f"""
        SELECT c.article_id, c.ord, c.heading, c.body, c.lang, c.anchor, a.title,
               a.source_id, a.raw_meta, a.body_md AS article_body_md,
               a.body_text AS article_body_text, a.ocr_text AS article_ocr_text, a.content_hash,
               md5({_SOURCE_TEXT}) AS hash
        FROM unnest(%s::bigint[], %s::int[]) AS k(article_id, ord)
        JOIN article_chunks c ON c.article_id = k.article_id AND c.ord = k.ord
        JOIN articles a ON a.id = c.article_id
        WHERE a.duplicate_of IS NULL AND {model_cache_safe_sql("a")}
          AND {visible_sql("a")}
          AND {chunk_provenance_sql("a", "c")}
        """,
        ([row["article_id"] for row in candidates], [row["ord"] for row in candidates]),
    ).fetchall()
    return [row for row in rows if _current_row_proven(conn, row)]


def _current_row_proven(conn, row: dict) -> bool:
    if not row.get("source_id") or "article_body_md" not in row:
        return False
    article = {
        "id": row["article_id"], "source_id": row["source_id"], "raw_meta": row.get("raw_meta"),
        "title": row.get("title"), "body_md": row["article_body_md"],
        "body_text": row.get("article_body_text") or "", "content_hash": row.get("content_hash"),
        "ocr_text": row.get("article_ocr_text") or "",
    }
    if is_suppressed(article["raw_meta"]) or not model_cache_safe_article(article):
        return False
    lang, ordinal = row.get("lang") or "", int(row.get("ord") or 0)
    if lang == "" and ordinal < 10000:
        return chunk_provenance_article(article, row)
    if lang == "zh" and 10000 <= ordinal < 20000:
        current = conn.execute(
            "SELECT content_hash, complete, title_zh, body_text FROM article_translations WHERE article_id = %s",
            (row["article_id"],),
        ).fetchone()
        return chunk_provenance_article(article, row, translation=current)
    if lang == "ocr" and ordinal >= 20000:
        current = conn.execute(
            "SELECT i.original_ref, i.content_hash, t.text FROM article_images i JOIN image_texts t "
            "ON t.content_hash = i.content_hash WHERE i.article_id = %s "
            "AND %s = 'img-' || left(i.content_hash, 12)",
            (row["article_id"], row.get("anchor") or ""),
        ).fetchall()
        return chunk_provenance_article(article, row, images=current)
    return False


def backfill(
    conn, *, limit: int | None = None, batch: int = BATCH, progress: Callable[[int], None] | None = None
) -> int:
    """给缺向量、或字改过的块算向量。删掉已经没有对应块的向量。返回这次算了多少块。"""
    kind = column_type(conn)
    if kind is None:
        raise EmbeddingError("数据库里没有 chunk_embeddings：先给 Postgres 装 pgvector（重建 db 镜像）")
    model = model_name() or "unknown"
    conn.execute(
        """
        DELETE FROM chunk_embeddings e
        WHERE NOT EXISTS (SELECT 1 FROM article_chunks c WHERE c.article_id = e.article_id AND c.ord = e.ord)
        """
    )
    conn.commit()
    done = 0
    while limit is None or done < limit:
        size = batch if limit is None else min(batch, limit - done)
        rows = _current_rows(conn, _pending(conn, size, model))
        if not rows:
            break
        texts = ["\n".join(part for part in (r["title"], r["heading"], r["body"]) if part) for r in rows]
        vectors = embed_texts(texts)
        with conn.cursor() as cur:
            cur.executemany(
                f"""
                INSERT INTO chunk_embeddings (article_id, ord, model, text_hash, embedding)
                VALUES (%s, %s, %s, %s, %s::{kind})
                ON CONFLICT (article_id, ord) DO UPDATE
                SET model = EXCLUDED.model, text_hash = EXCLUDED.text_hash,
                    embedding = EXCLUDED.embedding, updated_at = now()
                """,
                [(r["article_id"], r["ord"], model, r["hash"], literal(v)) for r, v in zip(rows, vectors, strict=True)],
            )
        conn.commit()
        done += len(rows)
        if progress:
            progress(done)
    return done


def coverage(conn) -> tuple[int, int]:
    """(有向量的块, 全部块)。"""
    if column_type(conn) is None:
        return 0, 0
    row = conn.execute(
        f"""
        SELECT (SELECT count(*) FROM chunk_embeddings e
                JOIN article_chunks c ON c.article_id = e.article_id AND c.ord = e.ord
                JOIN articles a ON a.id = c.article_id
                WHERE a.duplicate_of IS NULL AND {model_cache_safe_sql("a")}
                  AND {visible_sql("a")} AND {chunk_provenance_sql("a", "c")}
                  AND e.text_hash = md5({_SOURCE_TEXT})) AS done,
               (SELECT count(*) FROM article_chunks c JOIN articles a ON a.id = c.article_id
                WHERE a.duplicate_of IS NULL AND {model_cache_safe_sql("a")}
                  AND {visible_sql("a")} AND {chunk_provenance_sql("a", "c")}) AS total
        """
    ).fetchone()
    return int(row["done"]), int(row["total"])


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

        def report(done: int) -> None:
            if done % (args.batch * 20) == 0:
                print(f"已算 {done} 块，{time.monotonic() - started:.0f} 秒", flush=True)

        count = backfill(conn, limit=args.limit, batch=max(1, args.batch), progress=report)
        have, total = coverage(conn)
    except EmbeddingError as exc:
        print(f"向量回填失败：{exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(f"这次算了 {count} 块，库里 {have}/{total} 块有向量，用时 {time.monotonic() - started:.0f} 秒")
    return 0


if __name__ == "__main__":
    sys.exit(main())
