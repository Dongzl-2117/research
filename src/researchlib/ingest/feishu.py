"""飞书导出的 Markdown。目录可以嵌套，外部 id 用相对路径。

导入成功后按正文里的相对链接重写 doc_tree。没有根文档时只提示，不让导入失败。
不访问飞书 wiki。
"""

from __future__ import annotations

import sys

from researchlib.doctree import DocTreeError, build_doc_tree
from researchlib.ingest.cli import run_markdown_ingest


def _rebuild_tree(conn, source) -> None:
    try:
        stats = build_doc_tree(conn, source.id)
    except DocTreeError as exc:
        print(f"目录树没有生成：{exc}", file=sys.stderr)
        return
    print(f"目录树: {stats['nodes']} 篇，挂上 {stats['placed']}，未挂上 {stats['orphans']}")


def main(argv: list[str] | None = None) -> int:
    return run_markdown_ingest(
        "feishu",
        "导入 feishu2md 风格的嵌套 Markdown",
        argv,
        id_mode="path",
        after_store=_rebuild_tree,
    )


if __name__ == "__main__":
    sys.exit(main())
