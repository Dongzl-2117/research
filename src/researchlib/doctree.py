"""从正文里的相对链接还原飞书目录树。

归档是平的 `docs/*.md`，`raw_meta.parent` 一律是 `docs`，原来的知识库层级没有存下来。
不调用飞书 wiki API。从库里已有的相对 .md 链接推断：

- 根是标题或文件名「主页-Jason 林文丰」
- 从根做广度优先。一篇第一次被谁链接到，谁就是它的父节点，顺序按链接出现的次序
- 之后再出现的链接只是交叉引用，不改父节点

结果写进 `doc_tree`。页面在 `/sources/<来源>/tree`。

    research-doc-tree --source linwenfeng-feishu
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict, deque

from researchlib.mdlinks import MdIndex, iter_link_targets
from researchlib.sources import SourceRejected, load_sources, require_source
from researchlib.textutil import split_frontmatter

ROOT_TITLE = "主页-Jason 林文丰"

# 文库树只展示公司、行业研究。方法论、笔记、如何思考一类从树上藏掉（库里还在，检索和问库照常）。
# 「价值投资研究」是方法论，不算公司。

LIBRARY_KEEP = re.compile(r"(投资研究|行业研究)$|.+研究$|公用事业与周期业$")
LIBRARY_EXCLUDE = re.compile(r"^价值投资研究$")


def library_visible(title: str) -> bool:
    title = (title or "").strip()
    if not title or LIBRARY_EXCLUDE.match(title):
        return False
    return bool(LIBRARY_KEEP.search(title))


class DocTreeError(Exception):
    """目录树没生成。已有的树不动。"""


def _labels(row: dict) -> tuple[str, str, str]:
    title = str(row.get("title") or "").strip()
    filename = str((row.get("raw_meta") or {}).get("filename") or "")
    external = str(row.get("external_id") or "")
    stem = filename or external.rsplit("/", 1)[-1]
    stem = stem.rsplit(".", 1)[0] if stem.lower().endswith((".md", ".markdown")) else stem
    bare = stem.split("__", 1)[0].strip()
    return title, bare, stem


def find_root(rows: list[dict], root_title: str = ROOT_TITLE) -> dict | None:
    exact: list[dict] = []
    prefix: list[dict] = []
    for row in rows:
        labels = _labels(row)
        if root_title in labels:
            exact.append(row)
        elif any(label.startswith(root_title) for label in labels if label):
            prefix.append(row)
    pool = exact or prefix
    if not pool:
        return None
    pool.sort(key=lambda row: (len(str(row.get("title") or "")), int(row["id"])))
    return pool[0]


def _outgoing(row: dict, index: MdIndex) -> list[int]:
    _frontmatter, body = split_frontmatter(row.get("body_md") or "")
    parent = str((row.get("raw_meta") or {}).get("parent") or "")
    found: list[int] = []
    for href in iter_link_targets(body):
        article_id = index.resolve(href, parent)
        if article_id is None or article_id in found:
            continue
        found.append(article_id)
    return found


def infer_tree(rows: list[dict], root_title: str = ROOT_TITLE) -> tuple[int | None, list[dict]]:
    """返回 (根文章 id, [{article_id, parent_id, ord, orphan}])。没有根时根 id 是 None，边为空。"""
    if not rows:
        return None, []
    root = find_root(rows, root_title)
    if root is None:
        return None, []
    index = MdIndex(rows)
    links = {int(row["id"]): _outgoing(row, index) for row in rows}
    root_id = int(root["id"])
    parent_of: dict[int, int | None] = {root_id: None}
    order: dict[int, int] = {root_id: 0}
    queue: deque[int] = deque([root_id])
    while queue:
        current = queue.popleft()
        ordinal = 0
        for target in links.get(current, []):
            if target == current or target in parent_of:
                continue
            parent_of[target] = current
            order[target] = ordinal
            ordinal += 1
            queue.append(target)
    edges = [
        {"article_id": article_id, "parent_id": parent_id, "ord": order[article_id], "orphan": False}
        for article_id, parent_id in parent_of.items()
    ]
    orphans = [row for row in rows if int(row["id"]) not in parent_of]
    orphans.sort(key=lambda row: (str(row.get("title") or ""), int(row["id"])))
    for ordinal, row in enumerate(orphans):
        edges.append(
            {"article_id": int(row["id"]), "parent_id": None, "ord": ordinal, "orphan": True}
        )
    return root_id, edges


def _load_articles(conn, source_id: str) -> list[dict]:
    return conn.execute(
        """
        SELECT id, external_id, title, body_md, raw_meta
        FROM articles
        WHERE source_id = %s AND duplicate_of IS NULL
        ORDER BY id
        """,
        (source_id,),
    ).fetchall()


def build_doc_tree(conn, source_id: str, root_title: str = ROOT_TITLE) -> dict:
    """删掉这个来源的旧树，按当前正文重写。没有根文档时不改库。"""
    rows = _load_articles(conn, source_id)
    if not rows:
        raise DocTreeError("这个来源还没有文章")
    root_id, edges = infer_tree(rows, root_title)
    if root_id is None:
        raise DocTreeError(f"没有找到根文档「{root_title}」，目录树没有改")
    try:
        conn.execute("DELETE FROM doc_tree WHERE source_id = %s", (source_id,))
        for edge in edges:
            conn.execute(
                """
                INSERT INTO doc_tree (source_id, article_id, parent_id, ord, orphan)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (source_id, edge["article_id"], edge["parent_id"], edge["ord"], edge["orphan"]),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    orphans = sum(1 for edge in edges if edge["orphan"])
    return {
        "nodes": len(edges),
        "placed": len(edges) - orphans,
        "orphans": orphans,
        "root_id": root_id,
    }


def load_tree_rows(conn, source_id: str) -> list[dict]:
    return conn.execute(
        """
        SELECT t.article_id, t.parent_id, t.ord, t.orphan, a.title
        FROM doc_tree t
        JOIN articles a ON a.id = t.article_id
        WHERE t.source_id = %s
        ORDER BY t.ord, t.article_id
        """,
        (source_id,),
    ).fetchall()


def arrange_tree(rows: list[dict]) -> tuple[dict | None, list[dict]]:
    """嵌成 {id, title, children}。根是 parent 为空且不是孤儿的那篇。"""
    children_of: dict[int, list] = defaultdict(list)
    roots: list[dict] = []
    orphans: list[dict] = []
    for row in rows:
        if row["parent_id"] is None:
            if row["orphan"]:
                orphans.append(row)
            else:
                roots.append(row)
        else:
            children_of[int(row["parent_id"])].append(row)
    for kids in children_of.values():
        kids.sort(key=lambda item: (item["ord"], item["article_id"]))

    def node(row: dict, seen: set[int]) -> dict:
        article_id = int(row["article_id"])
        if article_id in seen:
            return {"id": article_id, "title": row["title"], "children": []}
        seen.add(article_id)
        return {
            "id": article_id,
            "title": row["title"],
            "children": [node(child, seen) for child in children_of.get(article_id, [])],
        }

    root = node(roots[0], set()) if roots else None
    return root, [node(row, set()) for row in orphans]


def filter_library_tree(root: dict | None, orphans: list[dict]) -> tuple[dict | None, list[dict]]:
    """只留「XX投资研究」「XX行业研究/XX研究」及其子文档。根（主页）保留当容器。

    孤儿和不匹配的顶层节点从树上藏掉，库里的文章、检索、问库不动。
    """
    if root is None:
        return None, []
    kept = [child for child in root.get("children") or [] if library_visible(child["title"])]
    return {**root, "children": kept}, []


def mark_open(node: dict, current: int | None) -> bool:
    """当前篇和它的祖先展开。返回这棵子树里有没有当前篇。"""
    hit = current is not None and node["id"] == current
    for child in node["children"]:
        hit = mark_open(child, current) or hit
    node["open"] = hit
    node["current"] = current is not None and node["id"] == current
    return hit


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按正文里的相对链接生成飞书目录树（不访问飞书）")
    parser.add_argument("--source", required=True, help="来源 id，例如 linwenfeng-feishu")
    parser.add_argument("--root", default=ROOT_TITLE, help="根文档标题")
    args = parser.parse_args(argv)
    from researchlib.config import Settings
    from researchlib.db import prepare

    settings = Settings.from_env()
    try:
        source = require_source(load_sources(settings.sources_path), args.source)
    except SourceRejected as exc:
        print(f"拒绝：{exc}", file=sys.stderr)
        return 2
    conn = prepare(settings)
    try:
        stats = build_doc_tree(conn, source.id, args.root)
    except DocTreeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(f"来源: {source.id}")
    print(f"目录: {stats['nodes']} 篇，挂上 {stats['placed']}，未挂上 {stats['orphans']}")
    print(f"页面: /sources/{source.id}/tree")
    return 0


if __name__ == "__main__":
    sys.exit(main())
