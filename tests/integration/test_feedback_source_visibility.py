"""反馈中的旧标题和链接也服从当前来源状态，历史标注保持不变。"""

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from researchlib import feedback
from researchlib.web.app import create_app


@pytest.mark.parametrize('check', ['read', 'write'])
def test_disabled_feedback_is_hidden_and_cannot_be_mutated(conn, settings, check):
    with TestClient(create_app(settings)) as client:
        assert client.get('/ask').status_code == 200
        ids = []
        for source, title in [('fangwei-mp', '合成可见反馈标题'), ('stratechery', '合成停用反馈标题')]:
            row = conn.execute(
                'INSERT INTO articles(source_id,external_id,content_hash,title,body_md,body_text)'
                " VALUES(%s,%s,'synthetic',%s,'合成正文。','合成正文。') RETURNING id", (source, title, title),
            ).fetchone()
            ids.append(row['id'])
        answer = conn.execute(
            "INSERT INTO llm_answers(question,scope,answer,citations,status)"
            " VALUES('合成有效问题',%s,'合成有效回答','[]','done') RETURNING id",
            (Jsonb({'source_ids': ['fangwei-mp']}),),
        ).fetchone()['id']
        conn.commit()
        feedback.add_missing(conn, answer, str(ids[0]), '可见备注')
        feedback.add_missing(conn, answer, str(ids[1]), '合成停用反馈备注')
        feedback.toggle_irrelevant(conn, answer, ids[1], 0)
        before = conn.execute('SELECT * FROM answer_feedback WHERE answer_id=%s ORDER BY id', (answer,)).fetchall()
        conn.execute("UPDATE sources SET enabled=false WHERE id='stratechery'")
        conn.commit()
        try:
            if check == 'read':
                labels = feedback.marks(conn, answer)
                assert [row['article_id'] for row in labels['missing']] == ids[:1]
                assert (ids[1], 0) not in labels['irrelevant']
                page = client.get(f'/ask/{answer}')
                assert page.status_code == 200 and '合成可见反馈标题' in page.text
                for text in ('合成停用反馈标题', '合成停用反馈备注', f'href="/articles/{ids[1]}"'):
                    assert text not in page.text
                payload = client.get(f'/api/v1/answers/{answer}')
                assert payload.status_code == 200 and '合成停用反馈标题' not in payload.text
            else:
                with pytest.raises(ValueError, match='库里没有'):
                    feedback.add_missing(conn, answer, str(ids[1]), '不得新增')
                with pytest.raises(ValueError, match='库里没有'):
                    feedback.toggle_irrelevant(conn, answer, ids[1], 0)
                missing = client.post(f'/ask/{answer}/feedback',
                                      data={'label': 'missing', 'ref': str(ids[1])}, follow_redirects=False)
                assert missing.status_code == 303 and 'feedback_error=absent' in missing.headers['location']
                irrelevant = client.post(f'/ask/{answer}/feedback',
                                         data={'label': 'irrelevant', 'article': str(ids[1]), 'chunk': '0'})
                assert irrelevant.status_code == 404
            assert conn.execute('SELECT * FROM answer_feedback WHERE answer_id=%s ORDER BY id',
                                (answer,)).fetchall() == before
        finally:
            conn.rollback()
            conn.execute("UPDATE sources SET enabled=true WHERE id='stratechery'")
            conn.commit()
        assert len(feedback.marks(conn, answer)['missing']) == 2
