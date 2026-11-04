"""侧栏只作为问库入口：真实数据库、合成资料和模型替身。"""

import pytest
from fastapi.testclient import TestClient

from researchlib import ask as ask_module
from researchlib.llm import LLMError
from researchlib.web.app import create_app


def _article(conn, external, source='fangwei-mp', chunks=None):
    chunks = chunks or ['渠道优势来自订阅分发，价格为10美元。Synthetic evidence. ' * 12]
    body = '\n\n'.join(chunks)
    identity = conn.execute(
        "INSERT INTO articles(source_id,external_id,content_hash,title,body_md,body_text)"
        " VALUES(%s,%s,%s,%s,%s,%s) RETURNING id",
        (source, external, external, '合成研究 '+external, body, body),
    ).fetchone()['id']
    for index, text in enumerate(chunks):
        conn.execute('INSERT INTO article_chunks(article_id,ord,heading,body) VALUES(%s,%s,%s,%s)',
                     (identity, index, f'合成章节{index}', text))
    conn.commit()
    return identity


@pytest.fixture
def model(monkeypatch):
    seen = []

    def fake_chat(_settings, messages, **_kwargs):
        seen.append(messages)
        if messages[0]['content'] == ask_module.EXPAND_PROMPT:
            return '{"同义":{"渠道":["分发"]}}', 'synthetic'
        return '订阅价格为99美元[1]。', 'synthetic'

    monkeypatch.setattr(ask_module, 'llm_configured', lambda _settings: True)
    monkeypatch.setattr(ask_module, 'chat', fake_chat)
    return seen


@pytest.mark.parametrize('restricted', [False, True])
def test_sidebar_async_and_original_sync_share_expansion_deep_evidence_and_checks(
    conn, settings, monkeypatch, model, restricted,
):
    identity = _article(conn, 'primary')
    _article(conn, 'other', 'fangwei-zsxq')
    calls = []
    real_retrieve = ask_module.retrieve

    def capture(*args, **kwargs):
        calls.append((kwargs['purpose'], args[2], kwargs.get('expansions')))
        return real_retrieve(*args, **kwargs)

    monkeypatch.setattr(ask_module, 'retrieve', capture)
    params = {'question': '渠道优势', 'source': 'fangwei-mp'}
    if restricted:
        params['article'] = identity
    with TestClient(create_app(settings)) as client:
        original = client.post('/api/v1/ask', json={**params, 'wait': True}).json()
        first = client.post('/api/v1/ask', json={**params, 'wait': False}).json()
        sidebar = client.get(f"/api/v1/answers/{first['id']}").json()
        assert original['status'] == sidebar['status'] == 'done'
        assert original['answer'] == sidebar['answer']
        assert [(p['article_id'], p['ord'], p['text']) for p in original['citations']] == [
            (p['article_id'], p['ord'], p['text']) for p in sidebar['citations']
        ]
        assert original['checks'] == sidebar['checks'] and sidebar['checks']
        assert sidebar['scope']['article_id'] == (identity if restricted else None)
        assert all(p['source_id'] == 'fangwei-mp' for p in sidebar['citations'])
        assert 'history' not in sidebar['scope'] and 'chat' not in sidebar['scope']
        assert sidebar['stats']['expansions'] and sidebar['stats']['passages']
        assert '渠道优势' in client.get('/ask').text
    assert [purpose for purpose, scope, expansion in calls] == ['display', 'deep', 'display', 'deep']
    assert all(scope.source_ids == ['fangwei-mp'] for purpose, scope, expansion in calls)
    assert all(expansion for purpose, scope, expansion in calls if purpose == 'deep')
    assert any(messages[0]['content'] == ask_module.EXPAND_PROMPT for messages in model)
    assert any(messages[0]['content'] == ask_module.SYSTEM_PROMPT for messages in model)


def test_sidebar_old_chat_contract_is_explicitly_rejected_instead_of_silently_narrowed(conn, settings, model):
    identity = _article(conn, 'legacy')
    with TestClient(create_app(settings)) as client:
        response = client.post('/api/v1/ask', json={
            'question': '渠道优势', 'article': identity, 'chat': True,
            'history': [{'role': 'assistant', 'content': '旧回答不能变成新证据[999]'}],
        })
    assert response.status_code == 400
    assert '刷新' in response.json()['error']['message']
    assert not model
    assert conn.execute('SELECT count(*) AS n FROM llm_answers').fetchone()['n'] == 0


def test_sidebar_no_model_preserves_raw_evidence_and_scope_errors(conn, settings, monkeypatch):
    identity = _article(conn, 'raw')
    _article(conn, 'outside', 'fangwei-zsxq')
    monkeypatch.setattr(ask_module, 'llm_configured', lambda _settings: False)
    with TestClient(create_app(settings)) as client:
        raw = client.post('/api/v1/ask', json={
            'question': '渠道优势', 'article': identity, 'source': 'fangwei-mp',
        }).json()
        assert raw['id'] is None and raw['citations'] and raw['error']
        assert {p['article_id'] for p in raw['citations']} == {identity}
        for params in ({'source': 'not-enabled'}, {'author': 'Ben Thompson', 'source': 'fangwei-mp'}):
            assert client.post('/api/v1/ask', json={'question': '渠道优势', **params}).status_code == 404
        mismatch = client.post('/api/v1/ask', json={
            'question': '渠道优势', 'article': identity, 'source': 'stratechery',
        }).json()
        assert mismatch['id'] is None and mismatch['citations'] == []
    assert conn.execute('SELECT count(*) AS n FROM llm_answers').fetchone()['n'] == 0


def test_sidebar_error_retry_uses_original_answer_id_job_and_retained_scope(conn, settings, monkeypatch, model):
    identity = _article(conn, 'retry')

    def failed(*args, **kwargs):
        raise LLMError('合成模型故障')

    monkeypatch.setattr(ask_module, 'chat', failed)
    with TestClient(create_app(settings)) as client:
        first = client.post('/api/v1/ask', json={
            'question': '渠道优势', 'article': identity, 'source': 'fangwei-mp',
        }).json()
        failed_row = client.get(f"/api/v1/answers/{first['id']}").json()
        assert failed_row['status'] == 'error'
        monkeypatch.setattr(ask_module, 'chat', lambda *a, **k: ('渠道优势来自订阅分发[1]。', 'synthetic'))
        retried = client.post(f"/ask/{first['id']}/retry", follow_redirects=False)
        assert retried.status_code == 303 and retried.headers['location'] == f"/ask/{first['id']}"
        saved = client.get(f"/api/v1/answers/{first['id']}").json()
        assert saved['status'] == 'done' and saved['scope'] == failed_row['scope']
        assert saved['stats']['passages'] and saved['citations']
    assert conn.execute('SELECT count(*) AS n FROM llm_answers').fetchone()['n'] == 1


def test_sidebar_long_summary_uses_original_deep_budget_and_batch_failure_report(conn, settings, monkeypatch):
    chunks = [(f'合成章节{index}：' + '订阅渠道优势与分发机制。' * 90) for index in range(80)]
    chunks[-1] += '合成文末唯一证据。'
    identity = _article(conn, 'long', chunks=chunks)
    assert sum(map(len, chunks)) > 60000
    seen_tail = []

    def fake_chat(_settings, messages, **_kwargs):
        body = messages[-1]['content']
        if messages[0]['content'] == ask_module.MAP_PROMPT:
            if '合成文末唯一证据' in body:
                seen_tail.append(True)
            if '合成章节0：' in body:
                raise LLMError('合成单批失败')
            return '订阅渠道优势[1]。', 'synthetic'
        return '订阅渠道优势[1]。', 'synthetic'

    monkeypatch.setattr(ask_module, 'llm_configured', lambda s: True)
    monkeypatch.setattr(ask_module, 'chat', fake_chat)
    with TestClient(create_app(settings)) as client:
        first = client.post('/api/v1/ask', json={
            'question': ask_module.PRESETS['article'], 'article': identity,
        }).json()
        saved = client.get(f"/api/v1/answers/{first['id']}").json()
    assert saved['status'] == 'done' and len(saved['citations']) == 80
    assert saved['stats']['batches'] >= 2 and saved['stats']['failed_batches']
    assert seen_tail


def test_sidebar_long_input_and_supplied_history_have_original_single_turn_semantics(conn, settings, model):
    identity = _article(conn, 'input')
    question = '渠道\x00优势' + '甲' * 600
    with TestClient(create_app(settings)) as client:
        first = client.post('/api/v1/ask', json={
            'question': question, 'article': identity,
            'history': [{'role': 'assistant', 'content': '历史秘密标记不要作为证据'}],
        }).json()
        saved = client.get(f"/api/v1/answers/{first['id']}").json()
    assert len(saved['question']) == 500 and '\x00' not in saved['question']
    assert 'history' not in saved['scope']
    assert all('历史秘密标记' not in message['content'] for messages in model for message in messages)


def test_sidebar_async_submission_uses_original_job_executor(conn, settings, monkeypatch, model):
    _article(conn, 'async')
    submitted = []
    monkeypatch.setattr(ask_module, '_inline', lambda: False)
    monkeypatch.setattr(ask_module._executor, 'submit', lambda *args: submitted.append(args))
    with TestClient(create_app(settings)) as client:
        first = client.post('/api/v1/ask', json={'question': '渠道优势', 'source': 'fangwei-mp'}).json()
    assert first['status'] == 'running' and first['citations']
    assert len(submitted) == 1 and submitted[0][0] is ask_module.run_job
    assert submitted[0][-1] == first['id']
