"""合成归档缺文件时保留已有本地图片；不下载图片、不调 OCR。"""

from datetime import UTC, datetime

import pytest

from researchlib.ingest.model import ParsedDoc
from researchlib.ingest.store import store_documents
from researchlib.sources import load_sources


def store(conn, settings, refs, *, extra='', include_image=True):
    source = next(item for item in load_sources(settings.sources_path) if item.id == 'fangwei-mp')
    body = '合成归档正文。' * 80 + ('\n\n![图表](figure.png)' if include_image else '') + extra
    return store_documents(conn, source=source, docs=[ParsedDoc(
        'synthetic-image-reimport', '合成图片归档', datetime(2026, 1, 1, tzinfo=UTC), body,
        {'format': 'markdown'}, refs,
    )], data_dir=settings.data_dir, archive_files=1, images_in_archive=0, errors=[])


def images(conn):
    return conn.execute('SELECT * FROM article_images ORDER BY original_ref').fetchall()


@pytest.mark.parametrize('mode', ['missing-path', 'no-reference-manifest'])
def test_unchanged_article_keeps_cached_image_when_export_is_incomplete(conn, settings, tmp_path, mode):
    path = tmp_path / 'figure.png'
    path.write_bytes(b'synthetic-image-content')
    assert store(conn, settings, [('figure.png', path)]).inserted == 1
    before = images(conn)
    assert len(before) == 1
    cache = settings.data_dir / 'images' / before[0]['storage_path']
    assert cache.read_bytes() == b'synthetic-image-content'
    path.unlink()
    refs = [('figure.png', path)] if mode == 'missing-path' else []
    result = store(conn, settings, refs)
    assert result.unchanged == 1 and not result.errors
    assert images(conn) == before
    assert result.images_linked == 1 and cache.is_file()


def test_missing_reference_stays_preserved_when_article_text_changes(conn, settings, tmp_path):
    path = tmp_path / 'figure.png'
    path.write_bytes(b'synthetic-image-content')
    store(conn, settings, [('figure.png', path)])
    before = images(conn)
    result = store(conn, settings, [('figure.png', None)], extra='\n\n新增的合成说明。')
    assert result.updated == 1 and not result.errors
    assert images(conn) == before


def test_explicit_article_change_can_remove_an_unlisted_reference(conn, settings, tmp_path):
    path = tmp_path / 'figure.png'
    path.write_bytes(b'synthetic-image-content')
    store(conn, settings, [('figure.png', path)])
    # 归档正文已经有真实改动；未再列出的附件按完整新清单处理。
    result = store(conn, settings, [], extra='\n\n这次归档明确更新了附件清单。', include_image=False)
    assert result.updated == 1 and not result.errors
    assert images(conn) == []


def test_available_new_image_still_updates_the_reference(conn, settings, tmp_path):
    path = tmp_path / 'figure.png'
    path.write_bytes(b'synthetic-first-image')
    store(conn, settings, [('figure.png', path)])
    before = images(conn)[0]
    path.write_bytes(b'synthetic-replacement-image')
    result = store(conn, settings, [('figure.png', path)])
    after = images(conn)[0]
    assert result.unchanged == 1 and not result.errors and len(images(conn)) == 1
    assert before['content_hash'] != after['content_hash']
    assert (settings.data_dir / 'images' / before['storage_path']).is_file()
    assert (settings.data_dir / 'images' / after['storage_path']).read_bytes() == b'synthetic-replacement-image'
