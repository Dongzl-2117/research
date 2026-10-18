"""语义检索的接缝。第一期不安装 pgvector，也不读取向量。

以后可以新增 article_embeddings 表（主键 articles.id），实现这个协议，
搜索页再加一路结果。articles 表本身不用改。
"""

from __future__ import annotations

from typing import Protocol


class EmbeddingIndex(Protocol):
    def index_article(self, article_id: int, text: str) -> None: ...

    def search(self, query: str, limit: int) -> list[int]: ...


class UnavailableEmbeddingIndex:
    def index_article(self, article_id: int, text: str) -> None:
        raise NotImplementedError(
            "语义向量检索还没启用。第一期只有中文全文检索；"
            "后续用单独的 article_embeddings 表接 pgvector。"
        )

    def search(self, query: str, limit: int) -> list[int]:
        raise NotImplementedError(
            "语义向量检索还没启用。第一期只有中文全文检索；"
            "后续用单独的 article_embeddings 表接 pgvector。"
        )
