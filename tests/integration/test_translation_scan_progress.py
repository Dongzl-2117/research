"""隔离库中的完整只读审计与分批重检进度；不请求模型。"""

import json

from researchlib.translate import audit, service


def _rows(conn, count):
    rows = conn.execute(
        "INSERT INTO articles(source_id,external_id,content_hash,title,body_md,body_text,published_at)"
        " SELECT 'stratechery','synthetic-scan-'||n,'synthetic-'||n,'合成审计'||n,"
        " 'Original English paragraph.','Original English paragraph.',"
        " CASE WHEN n<=2 THEN NULL ELSE '2026-01-01'::timestamptz END"
        " FROM generate_series(1,%s) n RETURNING id,content_hash", (count,),
    ).fetchall()
    for row in rows:
        conn.execute("INSERT INTO article_translations(article_id,content_hash,body_html,body_text,complete,segments)"
                     " VALUES(%s,%s,'<p>短中文。</p>','短中文。',true,1)", (row['id'], row['content_hash']))
    conn.commit()
    return [row['id'] for row in rows]


def test_audit_explicit_ids_are_not_truncated_by_default_or_small_limit(conn, settings, monkeypatch, capsys):
    ids = _rows(conn, 27)
    monkeypatch.setattr(audit.Settings, 'from_env', staticmethod(lambda: settings))
    monkeypatch.setattr(audit, '_render_original', lambda c, a, p: ('<p>Original English paragraph.</p>', 'synthetic'))
    args = [part for identity in ids for part in ('--article-id', str(identity))]
    assert audit.main([*args, '--limit', '1', '--dry-run']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['count'] == len(ids)
    assert {row['articleId'] for row in result['articles']} == set(ids)
    assert result['readOnly'] and result['modelCalls'] == 0
    assert conn.execute('SELECT count(*) AS n FROM article_translations').fetchone()['n'] == len(ids)


def test_recheck_advances_past_healthy_pages_with_equal_and_null_dates(conn, settings, monkeypatch):
    ids = _rows(conn, 6)
    monkeypatch.setattr(service, 'COARSE_SCAN_CAP', 2)
    # 后面才有需复核的旧稿；前几页都是正常短段。
    coarse = ''.join(f'<p>合成译文第{index}段。</p>' for index in range(50))
    conn.execute('UPDATE article_translations SET body_html=%s WHERE article_id=ANY(%s)', (coarse, ids[:2]))
    conn.commit()
    found = service.list_coarse_translations(conn, limit=1, source_id='stratechery')
    assert [row['id'] for row in found] == [ids[1]]
    assert [row['id'] for row in service.list_coarse_translations(conn, limit=10)] == [ids[1], ids[0]]
    conn.execute("UPDATE sources SET enabled=false WHERE id='stratechery'")
    conn.commit()
    try:
        assert service.list_coarse_translations(conn, limit=10) == []
    finally:
        conn.execute("UPDATE sources SET enabled=true WHERE id='stratechery'")
        conn.commit()
