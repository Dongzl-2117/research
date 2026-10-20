"""摘要、观点、论点调用失败的记次数：失败几次就隔几天再试（最多一周）。

不记的话，同一篇每次被打开都会重新排队、重新花一次推理；接口抽风的那几篇会反复失败。
成功写入时清掉记录。
"""

from __future__ import annotations

KINDS = ("digest", "views", "theses")
MAX_DAYS = 7


def note_failure(conn, kind: str, item_id: int, error: object) -> None:
    """记一次失败并提交。调用方先回滚掉失败的那次写入。"""
    if kind not in KINDS:
        raise ValueError(kind)
    conn.execute(
        """
        INSERT INTO llm_failures (kind, item_id, failures, last_error, last_at)
        VALUES (%s, %s, 1, %s, now())
        ON CONFLICT (kind, item_id) DO UPDATE SET
          failures = llm_failures.failures + 1,
          last_error = EXCLUDED.last_error,
          last_at = now()
        """,
        (kind, int(item_id), str(error)[:300]),
    )
    conn.commit()


def clear_failure(conn, kind: str, item_id: int) -> None:
    """成功了就清掉。和成功的写入在同一个事务里，由调用方提交。"""
    conn.execute("DELETE FROM llm_failures WHERE kind = %s AND item_id = %s", (kind, int(item_id)))


def cooling_sql(kind: str, column: str) -> str:
    """SQL 条件：这一项不在冷却期里。kind 只能是固定的几个值，column 是调用方写死的列名。"""
    if kind not in KINDS:
        raise ValueError(kind)
    return (
        "NOT EXISTS (SELECT 1 FROM llm_failures lf"
        f" WHERE lf.kind = '{kind}' AND lf.item_id = {column}"
        f" AND lf.last_at > now() - make_interval(days => least(lf.failures, {MAX_DAYS})))"
    )
