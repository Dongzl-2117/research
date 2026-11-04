"""隔离库中模拟过期、重试和迟到工作者；所有模型步骤为替身。"""

import time
from types import SimpleNamespace

import pytest

from researchlib import ask
from researchlib.db import connect
from researchlib.llm import LLMError
from researchlib.web.queries import Scope, expire_stale_answers


def setup_job(conn, monkeypatch):
    answer_id = conn.execute("INSERT INTO llm_answers(question,answer,status) VALUES('合成问题','','running')"
                             ' RETURNING id').fetchone()['id']
    conn.commit()
    found = SimpleNamespace(query=SimpleNamespace(overview=True, timeline=False), terms=[], outline=[],
                            outline_label='', scope=Scope(), companies=[], authors=[])
    monkeypatch.setattr(ask, 'analyze', lambda *a, **k: SimpleNamespace(terms=[]))
    monkeypatch.setattr(ask, 'expand_terms', lambda *a, **k: {})
    monkeypatch.setattr(ask, 'retrieve', lambda *a, **k: found)
    monkeypatch.setattr(ask, '_passages', lambda *a, **k: [{'n': 1, 'text': '合成资料。'}])
    monkeypatch.setattr(ask, 'outline_note', lambda *a: '')
    return answer_id


@pytest.mark.parametrize('old_outcome', ['success', 'error'])
def test_late_attempt_cannot_overwrite_a_successful_retry(conn, settings, monkeypatch, old_outcome):
    answer_id = setup_job(conn, monkeypatch)
    queued = []
    monkeypatch.setattr(ask._executor, 'submit', lambda fn, *a, **k: queued.append((fn, a, k)))
    calls = []

    def synthesize(*args, **kwargs):
        calls.append('call')
        if len(calls) > 1:
            return '新一轮完成[1]。', 'synthetic'
        other = connect(settings)
        try:
            other.execute("UPDATE llm_answers SET updated_at=now()-interval '700 seconds' WHERE id=%s", (answer_id,))
            other.commit()
            assert expire_stale_answers(other) == 1
            assert ask.retry(other, settings, answer_id, run_inline=False)
            fn, positional, named = queued.pop()
            fn(*positional, **named, conn=other)
        finally:
            other.close()
        if old_outcome == 'error':
            raise LLMError('旧工作者迟到错误')
        return '旧工作者迟到结果[1]。', 'synthetic'

    monkeypatch.setattr(ask, 'synthesize', synthesize)
    ask.run_job(settings, answer_id, conn=conn)
    row = conn.execute('SELECT answer,status,error FROM llm_answers WHERE id=%s', (answer_id,)).fetchone()
    assert row == {'answer': '新一轮完成[1]。', 'status': 'done', 'error': ''}


def test_second_worker_cannot_run_an_already_claimed_attempt(conn, settings, monkeypatch):
    answer_id = setup_job(conn, monkeypatch)
    calls = []

    def synthesize(*args, **kwargs):
        calls.append('call')
        if len(calls) == 1:
            other = connect(settings)
            try:
                ask.run_job(settings, answer_id, conn=other)
            finally:
                other.close()
        return '合成完成[1]。', 'synthetic'

    monkeypatch.setattr(ask, 'synthesize', synthesize)
    ask.run_job(settings, answer_id, conn=conn)
    assert calls == ['call']


def test_active_heartbeat_prevents_false_timeout_during_long_model_step(conn, settings, monkeypatch):
    answer_id = setup_job(conn, monkeypatch)
    monkeypatch.setattr(ask, 'HEARTBEAT_INTERVAL_S', .01, raising=False)

    def synthesize(*args, **kwargs):
        other = connect(settings)
        try:
            other.execute("UPDATE llm_answers SET updated_at=now()-interval '700 seconds' WHERE id=%s", (answer_id,))
            other.commit()
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                fresh = other.execute("SELECT updated_at > clock_timestamp()-interval '10 seconds' AS fresh"
                                      ' FROM llm_answers WHERE id=%s', (answer_id,)).fetchone()['fresh']
                other.commit()
                if fresh:
                    break
                time.sleep(.01)
            assert expire_stale_answers(other) == 0
        finally:
            other.close()
        return '合成完成[1]。', 'synthetic'

    monkeypatch.setattr(ask, 'synthesize', synthesize)
    ask.run_job(settings, answer_id, conn=conn)
    assert conn.execute('SELECT status FROM llm_answers WHERE id=%s', (answer_id,)).fetchone()['status'] == 'done'
