"""同一篇归档从网页、JSON、MCP 和 Markdown 取得时都能读到两个分隔线之间的判断。"""

import pytest
from fastapi.testclient import TestClient

from researchlib.ingest.archive import ImageIndex
from researchlib.ingest.parse import parse_markdown_files
from researchlib.ingest.store import store_documents
from researchlib.sources import load_sources
from researchlib.textutil import split_frontmatter
from researchlib.web.app import create_app


@pytest.mark.parametrize('start', ['---', '----', '---\ntitle: 经营跟踪\noriginal_marker: 来源元数据\n---'])
def test_download_preserves_evidence_between_markdown_rules(conn, settings, tmp_path, start):
    evidence = '经营假设：库存上升使经营现金流转负。'
    check = '下季复核存货周转天数。'
    path = tmp_path / '经营跟踪.md'
    path.write_text(f'{start}\n\n{evidence}\n\n---\n\n# 经营跟踪\n\n{check}', encoding='utf-8')
    docs, errors = parse_markdown_files(
        [path], roots=[tmp_path], images=ImageIndex([], [tmp_path]), id_mode='path',
    )
    assert not errors and len(docs) == 1
    source = next(s for s in load_sources(settings.sources_path) if s.id == 'linwenfeng-feishu')
    store_documents(conn, source=source, docs=docs, data_dir=settings.data_dir, archive_files=1,
                    images_in_archive=0, errors=[])
    article_id = conn.execute('SELECT id FROM articles WHERE external_id = %s', (docs[0].external_id,)).fetchone()['id']
    with TestClient(create_app(settings)) as client:
        page = client.get(f'/articles/{article_id}').text
        single = client.get(f'/api/v1/articles/{article_id}').json()
        mcp = client.post('/api/mcp', json={
            'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
            'params': {'name': 'get_article', 'arguments': {'id': article_id}},
        }).json()['result']['structuredContent']
        download = client.get(f'/api/v1/articles/{article_id}.md')
    assert download.status_code == 200
    front, body = split_frontmatter(download.text)
    assert front['id'] == article_id and front['title'] == '经营跟踪'
    assert 'original_marker' not in body
    for rendered in (page, single['body_markdown'], mcp['body_markdown'], body):
        assert evidence in rendered and check in rendered
