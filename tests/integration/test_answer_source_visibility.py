"""停用来源后，历史回答及原文快照不能继续从页面或API读到。"""

from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from researchlib.web import queries
from researchlib.web.app import create_app


def test_disabled_scope_or_citation_hides_history_without_deleting_it(conn, settings):
    with TestClient(create_app(settings)) as client:
        # 首个请求会从配置同步来源；随后只改变隔离库的当前停用状态。
        assert client.get('/ask').status_code == 200
        rows = []
        for scope, citations in [({'source_ids': ['stratechery']}, []),
                                 ({}, [{'n': 1, 'source_id': 'stratechery', 'text': '合成旧快照。'}])]:
            row = conn.execute("INSERT INTO llm_answers(question,scope,answer,citations,status)"
                               " VALUES('合成旧问题',%s,'合成旧回答',%s,'done') RETURNING id",
                               (Jsonb(scope), Jsonb(citations))).fetchone()
            rows.append(row['id'])
        conn.commit()
        assert all(queries.get_answer(conn, identity) for identity in rows)
        conn.execute("UPDATE sources SET enabled=false WHERE id='stratechery'")
        conn.commit()
        try:
            for identity in rows:
                assert queries.get_answer(conn, identity) is None
                assert client.get(f'/ask/{identity}').status_code == 404
                assert client.get(f'/api/v1/answers/{identity}').status_code == 404
            assert not any(item['id'] in rows for item in queries.recent_answers(conn))
            assert conn.execute('SELECT count(*) AS n FROM llm_answers WHERE id=ANY(%s)', (rows,)).fetchone()['n'] == 2
        finally:
            conn.execute("UPDATE sources SET enabled=true WHERE id='stratechery'")
            conn.commit()


def test_recent_answers_combines_source_visibility_and_sidebar_history_filter(conn):
    """同步主线时，两种过滤必须同时保留；允许查看的侧栏回答仍能按 id 读取。"""
    identities = {}
    for enabled in (False, True):
        for chat in (False, True):
            source = 'fangwei-mp' if enabled else 'stratechery'
            scope = {'source_ids': [source], 'chat': chat}
            row = conn.execute(
                'INSERT INTO llm_answers(question,scope,answer,citations,status)'
                " VALUES(%s,%s,'合成回答','[]','done') RETURNING id",
                (f'合成历史-{enabled}-{chat}', Jsonb(scope)),
            ).fetchone()
            identities[enabled, chat] = row['id']
    conn.execute("UPDATE sources SET enabled=false WHERE id='stratechery'")
    conn.commit()
    try:
        assert [row['id'] for row in queries.recent_answers(conn)] == [identities[True, False]]
        assert queries.get_answer(conn, identities[True, True]) is not None
        assert queries.get_answer(conn, identities[False, False]) is None
        assert queries.get_answer(conn, identities[False, True]) is None
        assert conn.execute('SELECT count(*) AS n FROM llm_answers').fetchone()['n'] == 4
    finally:
        conn.execute("UPDATE sources SET enabled=true WHERE id='stratechery'")
        conn.commit()
