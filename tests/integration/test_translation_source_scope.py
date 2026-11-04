"""隔离测试库验证停用来源在队列、显式请求和写回前都受约束。"""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from researchlib.db import connect
from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.sources import load_sources
from researchlib.translate import audit, cli, service
from researchlib.translate.llm import ChatJsonResult
from researchlib.translate.provenance import AlignmentError

BODY = '<p>First paragraph 10 dollars.</p><p>Second paragraph 20 dollars.</p>'


def store(conn, settings, source_id, *, newer=False):
    source = next(s for s in load_sources(settings.sources_path) if s.id == source_id)
    source = replace(source, min_chars=0)
    store_documents(conn, source=source, docs=[ParsedDoc(
        'synthetic-source-scope', 'Synthetic scope', datetime(2026, 1, 2 if newer else 1, tzinfo=UTC),
        BODY, {'format': 'markdown', 'language': 'en'}, [],
    )], data_dir=settings.data_dir, archive_files=1, images_in_archive=0, errors=[])
    return conn.execute('SELECT id FROM articles WHERE source_id=%s', (source_id,)).fetchone()['id']


def model(**kwargs):
    user = kwargs['user']
    values = [{'id': identity, 'source_hash': source_hash,
               'html': html.replace('First paragraph ', '第一段').replace('Second paragraph ', '第二段')
               .replace(' dollars', '美元').replace('Synthetic scope', '合成范围')}
              for identity, source_hash, html in zip(user['segment_ids'], user['source_hashes'],
                                                       user['segments'], strict=True)]
    return ChatJsonResult(data={'t': values}, model='synthetic', usage=None, latency_ms=0)


def set_enabled(conn, source_id, enabled):
    conn.execute('UPDATE sources SET enabled=%s WHERE id=%s', (enabled, source_id))
    conn.commit()


def test_pending_filters_disabled_sources_before_limit_even_when_explicit_source(conn, settings):
    enabled_id = store(conn, settings, 'stratechery')
    disabled_id = store(conn, settings, 'fangwei-mp', newer=True)
    set_enabled(conn, 'fangwei-mp', False)
    try:
        assert service.list_pending_english(conn, limit=1) == [enabled_id]
        assert service.list_pending_english(conn, source_id='fangwei-mp') == []
        assert disabled_id not in service.list_pending_english(conn, limit=100)
    finally:
        set_enabled(conn, 'fangwei-mp', True)


def test_recheck_filters_disabled_sources_before_scan_cap(conn, settings, monkeypatch):
    enabled_id = store(conn, settings, 'stratechery')
    disabled_id = store(conn, settings, 'fangwei-mp', newer=True)
    conn.execute('INSERT INTO article_translations(article_id,content_hash,body_html,body_text,complete,segments)'
                 ' SELECT id,content_hash,%s,%s,true,1 FROM articles', (BODY, BODY))
    conn.commit()
    monkeypatch.setattr(service, 'coarse_segment_reason', lambda *a, **kw: 'synthetic-coarse')
    monkeypatch.setattr(service, 'COARSE_SCAN_CAP', 1)
    set_enabled(conn, 'fangwei-mp', False)
    try:
        assert service.list_coarse_translations(conn, limit=1) == [
            {'id': enabled_id, 'source': 'stratechery', 'reason': 'synthetic-coarse'}]
        assert service.list_coarse_translations(conn, source_id='fangwei-mp') == []
        assert disabled_id != enabled_id
    finally:
        set_enabled(conn, 'fangwei-mp', True)


def test_explicit_article_from_disabled_source_skips_before_model_or_write(conn, settings, monkeypatch):
    article_id = store(conn, settings, 'stratechery')
    set_enabled(conn, 'stratechery', False)
    monkeypatch.setattr(service, 'chat_json', lambda **kw: pytest.fail('停用来源不能调用模型'))
    try:
        result = service.translate_article(conn, article_id, data_dir=settings.data_dir)
        assert result.status == 'skipped' and result.reason == 'source disabled'
        assert conn.execute('SELECT count(*) AS n FROM article_translations').fetchone()['n'] == 0
        assert conn.execute('SELECT count(*) AS n FROM translation_attempts').fetchone()['n'] == 0
    finally:
        set_enabled(conn, 'stratechery', True)


def test_disable_during_request_preserves_translation_and_search_chunks(conn, settings, monkeypatch):
    article_id = store(conn, settings, 'stratechery')
    monkeypatch.setattr(service, 'chat_json', model)
    service.translate_article(conn, article_id, data_dir=settings.data_dir)
    before = conn.execute('SELECT * FROM article_translations WHERE article_id=%s', (article_id,)).fetchone()
    chunks = conn.execute("SELECT * FROM article_chunks WHERE article_id=%s AND lang='zh' ORDER BY ord",
                          (article_id,)).fetchall()
    other = connect(settings)
    disabled = False

    def disable(**kwargs):
        nonlocal disabled
        if not disabled:
            set_enabled(other, 'stratechery', False)
            disabled = True
        return model(**kwargs)

    monkeypatch.setattr(service, 'chat_json', disable)
    try:
        with pytest.raises(AlignmentError, match='source-disabled-during-request'):
            service.translate_article(conn, article_id, data_dir=settings.data_dir)
        conn.rollback()
        current = conn.execute('SELECT * FROM article_translations WHERE article_id=%s', (article_id,)).fetchone()
        assert current == before
        assert conn.execute("SELECT * FROM article_chunks WHERE article_id=%s AND lang='zh' ORDER BY ord",
                            (article_id,)).fetchall() == chunks
    finally:
        conn.rollback()
        set_enabled(other, 'stratechery', True)
        other.close()


def test_historical_plan_marks_target_edits_without_writing_them(conn, settings, monkeypatch, capsys):
    article_id = store(conn, settings, 'stratechery')
    monkeypatch.setattr(service, 'chat_json', model)
    service.translate_article(conn, article_id, data_dir=settings.data_dir)
    conn.execute("UPDATE article_translations SET body_html=replace(body_html,'第一段','第一段人工修订')"
                 ' WHERE article_id=%s', (article_id,))
    conn.commit()
    before = conn.execute('SELECT * FROM article_translations WHERE article_id=%s', (article_id,)).fetchone()
    monkeypatch.setattr(audit.Settings, 'from_env', staticmethod(lambda: settings))
    assert audit.main(['--article-id', str(article_id), '--dry-run']) == 0
    output = capsys.readouterr().out
    assert '"category": "target-conflict"' in output and '"action": "review-target-edits"' in output
    assert '人工修订' not in output and '"writes": false' in output
    assert conn.execute('SELECT * FROM article_translations WHERE article_id=%s', (article_id,)).fetchone() == before


def test_explicit_recheck_dry_run_matches_only_named_article_in_real_database(conn, settings, monkeypatch, capsys):
    article_id = store(conn, settings, 'stratechery')
    other_id = store(conn, settings, 'fangwei-mp', newer=True)
    monkeypatch.setattr(cli.Settings, 'from_env', staticmethod(lambda: settings))
    for name in ('prepare', 'translate_article', 'llm_provider', 'deepseek_api_key'):
        monkeypatch.setattr(cli, name, lambda *a, **kw: pytest.fail('定向干跑不应调用迁移或模型'))
    assert cli.main(['--recheck', '--dry-run', '--article-id', str(article_id)]) == 0
    output = capsys.readouterr().out
    assert f'#{article_id} stratechery explicit-article' in output
    assert f'#{other_id} ' not in output
    assert conn.execute('SELECT count(*) AS n FROM article_translations').fetchone()['n'] == 0
    assert conn.execute('SELECT count(*) AS n FROM translation_attempts').fetchone()['n'] == 0
