"""同一篇从两条路进来时只留一个主版本。

AIHOT 导入的外部 id 是 aihot:<原 id>，文件归档用相对路径或 topic_id，同一篇两边各导一次就是两行；
同一位作者在公众号和知识星球之间转发也是。不去重的话首页会出现两遍、公司提及篇数虚高，
问库里同一段原文占两个名额，模型还会以为「几篇都这么说」。

怎么认：正文按句切开，只留汉字、字母和数字，每句算一个 64 位哈希。每篇存哈希最小的 32 句当指纹。
同一位作者、发布时间前后一周内的文章里，指纹重合够多的才逐句核对：较短那篇至少 80% 的句子
（且至少 3 句）出现在另一篇里，两篇长度也差不多（短帖要求更接近），才算同一篇。
同一位作者很多篇都有的句子（页脚、免责声明、口头禅）不算。

主版本：正文更全更长的（转发时加了点评的那版优先）、再看图片、发得早的、id 小的。其余的 duplicate_of 指向它，
段落块、公司提及和观点都删掉，不进首页、作者页、公司页、检索和问库；文章页还能打开。
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import re
import sys
from dataclasses import dataclass

logger = logging.getLogger("researchlib.duplicates")

SKETCH = 32
MIN_SENTENCE = 12
MIN_WHOLE = 20
CANDIDATE_SHARED = 4
CONTAINMENT = 0.8
LENGTH_RATIO = 0.5
# 两边句子并集里重合够多才算同一篇，避免短文被长文里的社群广告段「吞掉」。
JACCARD = 0.55
SHORT_POST = 5
SHORT_LENGTH_RATIO = 0.8
# 同一位作者超过这么多篇都有的句子当作页脚、套话，不拿来认重复。
COMMON_DF = 6
# 两篇都有发布时间时，只在前后这么多天里找同一篇。
WINDOW_DAYS = 7
VERSION = "2"
STATE_KEY = "dedupe_version"

_KEEP = re.compile(r"[0-9A-Za-z一-鿿]+")
_SPLIT = re.compile(r"[。！？!?；;\n]+")


def normalize(text: str) -> str:
    return "".join(_KEEP.findall(text or "")).lower()


def _hash(text: str) -> int:
    return int.from_bytes(hashlib.blake2b(text.encode("utf-8"), digest_size=8).digest(), "big", signed=True)


@dataclass
class Fingerprint:
    sentences: set[int]
    sketch: list[int]
    length: int


def fingerprint(body_text: str) -> Fingerprint:
    hashes: set[int] = set()
    for piece in _SPLIT.split(body_text or ""):
        sentence = normalize(piece)
        if len(sentence) >= MIN_SENTENCE:
            hashes.add(_hash(sentence))
    whole = normalize(body_text)
    if hashes:
        sketch = sorted(hashes)[:SKETCH]
    elif len(whole) >= MIN_WHOLE:
        # 没有一句够长的短帖：拿全文当一句。
        hashes = {_hash(whole)}
        sketch = sorted(hashes)
    else:
        sketch = []
    return Fingerprint(hashes, sketch, len(whole))


def same_article(a: Fingerprint, b: Fingerprint, common: set[int] = frozenset()) -> bool:
    left = a.sentences - common
    right = b.sentences - common
    if not left or not right:
        return False
    smaller = min(len(left), len(right))
    shared = len(left & right)
    if shared / smaller < CONTAINMENT or shared < min(3, smaller):
        return False
    union = len(left | right)
    if union and shared / union < JACCARD:
        return False
    shortest = min(a.length, b.length)
    longest = max(a.length, b.length) or 1
    floor = SHORT_LENGTH_RATIO if smaller < SHORT_POST else LENGTH_RATIO
    return shortest / longest >= floor


def similarity(left_text: str, right_text: str) -> dict:
    """给核对页看的重合程度：较短那篇的句子有多少在另一篇里、两篇长度之比。不扣掉页脚套话，只作参考。"""
    left = fingerprint(left_text)
    right = fingerprint(right_text)
    shared = len(left.sentences & right.sentences)
    smaller = min(len(left.sentences), len(right.sentences)) or 1
    longest = max(left.length, right.length) or 1
    return {"containment": shared / smaller, "length_ratio": min(left.length, right.length) / longest}


def store_fingerprint(conn, article_id: int, content_hash: str, found: Fingerprint) -> None:
    conn.execute("DELETE FROM article_fingerprints WHERE article_id = %s", (article_id,))
    if found.sketch:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO article_fingerprints (article_id, h) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                [(article_id, value) for value in found.sketch],
            )
    conn.execute("UPDATE articles SET fingerprint_of = %s WHERE id = %s", (content_hash, article_id))


def _article(conn, article_id: int):
    return conn.execute(
        """
        SELECT a.id, a.source_id, a.body_text, a.content_hash, a.duplicate_of, a.published_at, a.keep_separate,
               s.author,
               (SELECT count(*) FROM article_images i WHERE i.article_id = a.id) AS images
        FROM articles a JOIN sources s ON s.id = a.source_id
        WHERE a.id = %s
        """,
        (article_id,),
    ).fetchone()


def _common_hashes(conn, author: str, hashes: list[int]) -> set[int]:
    if not hashes:
        return set()
    rows = conn.execute(
        """
        SELECT f.h
        FROM article_fingerprints f
        JOIN articles a ON a.id = f.article_id
        JOIN sources s ON s.id = a.source_id
        WHERE f.h = ANY(%s) AND s.author = %s
        GROUP BY f.h
        HAVING count(*) > %s
        """,
        (hashes, author, COMMON_DF),
    ).fetchall()
    return {row["h"] for row in rows}


def _candidates(
    conn, article_id: int, author: str, sketch: list[int], common: set[int], published,
    source_id: str | None = None,
) -> list[int]:
    usable = [value for value in sketch if value not in common]
    if not usable:
        return []
    # 短帖的指纹本来就少，另一份多出一两句就会凑不满；至少共享一半就拿出来核对（核对才是真正的判断）。
    need = min(CANDIDATE_SHARED, max(1, (len(usable) + 1) // 2))
    # 两边都有发布时间：只看前后一周。缺日期的文库（比如飞书）不放开时间窗去扫全作者，
    # 只和同一来源、同样没日期的文章比，避免大批误并。
    rows = conn.execute(
        """
        SELECT f.article_id
        FROM article_fingerprints f
        JOIN articles a ON a.id = f.article_id
        JOIN sources s ON s.id = a.source_id
        WHERE f.h = ANY(%(hashes)s) AND f.article_id <> %(id)s AND s.author = %(author)s
          AND NOT a.keep_separate
          AND (
            (%(published)s::timestamptz IS NOT NULL AND a.published_at IS NOT NULL
             AND a.published_at BETWEEN %(published)s::timestamptz - make_interval(days => %(window)s)
                                    AND %(published)s::timestamptz + make_interval(days => %(window)s))
            OR (%(published)s::timestamptz IS NULL AND a.published_at IS NULL
                AND a.source_id = %(source_id)s)
          )
        GROUP BY f.article_id
        HAVING count(*) >= %(need)s
        ORDER BY count(*) DESC
        LIMIT 50
        """,
        {"hashes": usable, "id": article_id, "author": author, "need": need, "published": published,
         "window": WINDOW_DAYS, "source_id": source_id},
    ).fetchall()
    return [row["article_id"] for row in rows]


def duplicates_of(
    conn, article_id: int, found: Fingerprint, author: str, published=None, source_id: str | None = None
) -> list[int]:
    """同一作者里和这篇是同一篇的文章（核对过的）。"""
    # 候选阶段只用本篇句子滤套话；逐篇核对时和对方句子并起来再滤，避免长短文不对称。
    common_seed = _common_hashes(conn, author, sorted(found.sentences)[:400])
    matches = []
    for other_id in _candidates(conn, article_id, author, found.sketch, common_seed, published, source_id):
        other = conn.execute("SELECT body_text FROM articles WHERE id = %s", (other_id,)).fetchone()
        if other is None:
            continue
        other_fp = fingerprint(other["body_text"])
        common = _common_hashes(conn, author, sorted(found.sentences | other_fp.sentences)[:800])
        if same_article(found, other_fp, common):
            matches.append(other_id)
    return matches


def _ranker(rows):
    """越小越该当主版本：明显更全的正文优先（长度不到最长那份的四分之三算不全，比如半截稿）；
    再比正文更长的（星球转发时加了点评的那版要留下，不能因为图少被藏掉）；
    再比图片多的；发得早的；先入库的。"""
    lengths = {row["id"]: len(normalize(row["body_text"])) for row in rows}
    longest = max(lengths.values(), default=0) or 1

    def rank(row) -> tuple:
        published = row["published_at"]
        return (
            lengths[row["id"]] < 0.75 * longest,
            -lengths[row["id"]],
            -int(row["images"] or 0),
            published is None,
            published.timestamp() if published is not None else 0.0,
            int(row["id"]),
        )

    return rank


def _strip(conn, article_id: int) -> None:
    for table in ("article_chunks", "article_mentions", "company_views"):
        conn.execute(f"DELETE FROM {table} WHERE article_id = %s", (article_id,))


def _restore(conn, article_id: int, matcher) -> None:
    from researchlib.chunks import replace_chunks, replace_ocr_chunks, replace_translation_chunks
    from researchlib.mentions import mentions_for_document, replace_mentions

    row = conn.execute("SELECT title, body_md FROM articles WHERE id = %s", (article_id,)).fetchone()
    if row is None:
        return
    replace_chunks(conn, article_id, row["title"], row["body_md"])
    replace_translation_chunks(conn, article_id)
    replace_ocr_chunks(conn, article_id)
    replace_mentions(conn, article_id, mentions_for_document(matcher, row["title"], row["body_md"]))


def _cluster(conn, seeds: set[int]) -> set[int]:
    """这些文章所在的整组：它们的主版本，以及已经指向这些主版本的重复。"""
    members = set(seeds)
    rows = conn.execute("SELECT id, duplicate_of FROM articles WHERE id = ANY(%s)", (sorted(members),)).fetchall()
    roots = {row["duplicate_of"] or row["id"] for row in rows}
    members |= roots
    for row in conn.execute(
        "SELECT id FROM articles WHERE duplicate_of = ANY(%s)", (sorted(members),)
    ).fetchall():
        members.add(row["id"])
    return members


def apply_cluster(conn, members: set[int], matcher) -> tuple[int, list[int]]:
    """在一组候选里挑主版本；只有和主版本直接核对为同一篇的才标重复，避免 A≈B、B≈C 却把 A 和 C 并掉。"""
    rows = [row for row in (_article(conn, member) for member in sorted(members)) if row is not None]
    by_id = {row["id"]: row for row in rows}
    # 先在组里找一个更全的当主版本，再只收与它真正相同的。
    canonical = min(rows, key=_ranker(rows))["id"]
    can_fp = fingerprint(by_id[canonical]["body_text"])
    author = by_id[canonical]["author"]
    direct = {canonical}
    for row in rows:
        if row["id"] == canonical:
            continue
        other_fp = fingerprint(row["body_text"])
        common = _common_hashes(conn, author, sorted(can_fp.sentences | other_fp.sentences)[:800])
        if same_article(can_fp, other_fp, common):
            direct.add(row["id"])
    became: list[int] = []
    for row in rows:
        if row["id"] in direct:
            want = None if row["id"] == canonical else canonical
        else:
            # 只是传递连上的，放开，不当重复。
            want = None
        if row["duplicate_of"] == want:
            continue
        # 什么时候认出来的：去重核对页按这个排，放开时清掉。
        conn.execute(
            "UPDATE articles SET duplicate_of = %s,"
            " duplicate_marked_at = CASE WHEN %s::bigint IS NULL THEN NULL ELSE now() END WHERE id = %s",
            (want, want, row["id"]),
        )
        if want is None:
            if row["duplicate_of"] is not None:
                _restore(conn, row["id"], matcher)
        else:
            if row["duplicate_of"] is None:
                _strip(conn, row["id"])
            became.append(row["id"])
    others = [item for item in direct if item != canonical]
    if others:
        conn.execute(
            "UPDATE articles SET duplicate_of = %s WHERE duplicate_of = ANY(%s) AND id <> %s",
            (canonical, others, canonical),
        )
    return canonical, became


def _release(conn, article_id: int, matcher) -> None:
    """改过正文、不再和原来那篇是同一篇的重复：恢复成独立的一篇。"""
    conn.execute("UPDATE articles SET duplicate_of = NULL, duplicate_marked_at = NULL WHERE id = %s", (article_id,))
    _restore(conn, article_id, matcher)


def resolve_article(conn, article_id: int, matcher) -> int | None:
    """入库后调用：算指纹，找同一篇，挑主版本。返回这篇最终指向的主版本（自己是主版本时返回 None）。"""
    row = _article(conn, article_id)
    if row is None:
        return None
    found = fingerprint(row["body_text"])
    store_fingerprint(conn, article_id, row["content_hash"], found)
    if row["keep_separate"]:
        if row["duplicate_of"] is not None:
            _release(conn, article_id, matcher)
        return None
    matches = duplicates_of(conn, article_id, found, row["author"], row["published_at"], row["source_id"])
    if not matches:
        if row["duplicate_of"] is not None:
            _release(conn, article_id, matcher)
        return None
    # 只并入匹配到的那几篇所在的组；这篇以前若属于别的组，不把旧组一起拖进来。
    apply_cluster(conn, _cluster(conn, set(matches)) | {article_id}, matcher)
    final = conn.execute("SELECT duplicate_of FROM articles WHERE id = %s", (article_id,)).fetchone()
    return final["duplicate_of"] if final else None


class _Union:
    def __init__(self) -> None:
        self.parent: dict[int, int] = {}

    def find(self, item: int) -> int:
        self.parent.setdefault(item, item)
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, left: int, right: int) -> None:
        a, b = self.find(left), self.find(right)
        if a != b:
            self.parent[max(a, b)] = min(a, b)


def rebuild_duplicates(conn, matcher=None) -> dict:
    """全库重认一遍。先补算指纹，再两两核对、按组挑主版本。返回计数。"""
    from researchlib.mentions import CompanyMatcher, load_company_rows

    matcher = matcher or CompanyMatcher(load_company_rows(conn))
    stale = [
        row["id"]
        for row in conn.execute(
            "SELECT id FROM articles WHERE fingerprint_of IS DISTINCT FROM content_hash ORDER BY id"
        ).fetchall()
    ]
    for index, article_id in enumerate(stale, start=1):
        row = conn.execute("SELECT body_text, content_hash FROM articles WHERE id = %s", (article_id,)).fetchone()
        store_fingerprint(conn, article_id, row["content_hash"], fingerprint(row["body_text"]))
        if index % 200 == 0:
            conn.commit()
    conn.commit()
    groups = _Union()
    ids = [row["id"] for row in conn.execute("SELECT id FROM articles ORDER BY id").fetchall()]
    for article_id in ids:
        row = _article(conn, article_id)
        if row["keep_separate"]:
            continue
        found = fingerprint(row["body_text"])
        for other_id in duplicates_of(conn, article_id, found, row["author"], row["published_at"], row["source_id"]):
            groups.union(article_id, other_id)
    clusters: dict[int, set[int]] = {}
    for article_id in ids:
        clusters.setdefault(groups.find(article_id), set()).add(article_id)
    marked = released = 0
    for members in clusters.values():
        if len(members) > 1:
            _canonical, became = apply_cluster(conn, members, matcher)
            marked += len(became)
        else:
            (only,) = members
            current = conn.execute("SELECT duplicate_of FROM articles WHERE id = %s", (only,)).fetchone()
            if current and current["duplicate_of"] is not None:
                _release(conn, only, matcher)
                released += 1
        conn.commit()
    conn.execute(
        """
        INSERT INTO app_state (key, value, updated_at) VALUES (%s, %s, now())
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
        """,
        (STATE_KEY, VERSION),
    )
    conn.commit()
    total = conn.execute("SELECT count(*) AS n FROM articles WHERE duplicate_of IS NOT NULL").fetchone()["n"]
    return {
        "articles": len(ids),
        "fingerprinted": len(stale),
        "marked": marked,
        "released": released,
        "duplicates": total,
    }


def dedupe_if_needed(settings) -> dict | None:
    """应用启动后在后台跑：第一次部署或算法版本变了就全库重认；平时只补没算过指纹的文章。"""
    from researchlib.db import connect, ensure_search_session
    from researchlib.mentions import CompanyMatcher, load_company_rows

    conn = connect(settings)
    # 恢复成主版本的文章要重切段落块，全文索引得用和别处一样的分词设置。
    ensure_search_session(conn)
    try:
        state = conn.execute("SELECT value FROM app_state WHERE key = %s", (STATE_KEY,)).fetchone()
        if state is None or state["value"] != VERSION:
            has_articles = conn.execute("SELECT EXISTS (SELECT 1 FROM articles) AS e").fetchone()["e"]
            if not has_articles:
                return None
            logger.warning("全库认一遍重复文章")
            result = rebuild_duplicates(conn)
            logger.warning("重复文章认完：%s", result)
            return result
        stale = [
            row["id"]
            for row in conn.execute(
                "SELECT id FROM articles WHERE fingerprint_of IS DISTINCT FROM content_hash ORDER BY id"
            ).fetchall()
        ]
        if not stale:
            return None
        matcher = CompanyMatcher(load_company_rows(conn))
        for article_id in stale:
            resolve_article(conn, article_id, matcher)
            conn.commit()
        return {"fingerprinted": len(stale)}
    finally:
        conn.close()


def separate(conn, article_id: int, matcher) -> bool:
    """认错了：把这篇标成独立的一篇，恢复它的段落块和提及；以后不再参与认重复。"""
    row = conn.execute("SELECT id, duplicate_of FROM articles WHERE id = %s", (article_id,)).fetchone()
    if row is None:
        return False
    conn.execute("UPDATE articles SET keep_separate = true WHERE id = %s", (article_id,))
    if row["duplicate_of"] is not None:
        _release(conn, article_id, matcher)
    # 以它为主版本的重复也放开，重新认一遍（它们之间可能仍是同一篇）。
    for other in conn.execute("SELECT id FROM articles WHERE duplicate_of = %s", (article_id,)).fetchall():
        _release(conn, other["id"], matcher)
        conn.execute("UPDATE articles SET fingerprint_of = NULL WHERE id = %s", (other["id"],))
    conn.commit()
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="全库重新认一遍重复文章（同一篇从不同路径导入或在不同来源转发）")
    parser.add_argument("--separate", type=int, action="append", default=[], metavar="ID",
                        help="认错了：把这篇标成独立的一篇，以后不再参与认重复，可重复")
    args = parser.parse_args(argv)
    from researchlib.config import Settings
    from researchlib.db import prepare
    from researchlib.mentions import CompanyMatcher, load_company_rows

    conn = prepare(Settings.from_env())
    try:
        if args.separate:
            matcher = CompanyMatcher(load_company_rows(conn))
            for article_id in args.separate:
                print(f"{article_id}：{'已标成独立的一篇' if separate(conn, article_id, matcher) else '没有这篇'}")
            return 0
        result = rebuild_duplicates(conn)
    finally:
        conn.close()
    print(
        f"共 {result['articles']} 篇，补算指纹 {result['fingerprinted']} 篇，"
        f"这次新认出重复 {result['marked']} 篇，恢复 {result['released']} 篇，现有重复 {result['duplicates']} 篇"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
