"""主线段落合并标记必须可重复、局部且不受并发解析影响。"""

from concurrent.futures import ThreadPoolExecutor

import pytest

from researchlib.translate.provenance import positional_numbers_conflict, receipt, source_manifest, verify

BODY = '<p>' + ' '.join(
    f'Synthetic topic {i} records an observation about distribution advantages and regional demand.'
    for i in range(20)
) + '</p>'


def test_same_source_receipt_is_stable_after_other_materializations():
    first = source_manifest(BODY)
    source_manifest(BODY.replace('regional', 'local'))
    second = source_manifest(BODY)
    assert first == second
    proof = receipt(first, first['source_snapshot'])
    assert verify(BODY, first['source_snapshot'], proof).status == 'mapped'


def test_source_paragraph_groups_are_local_and_stable_under_concurrent_parsing():
    body = BODY + BODY.replace('Synthetic topic', 'Independent topic')
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(source_manifest, [body] * 12))
    assert all(value == results[0] for value in results)
    import re

    groups = re.findall(r'data-src-para="(\d+)"', results[0]['source_snapshot'])
    assert len(set(groups)) == 2


@pytest.mark.parametrize('source', ['Q1 FY2025', 'FY2025 Q1', 'Q1FY2025',
                                    'fiscal year 2025 q1', 'Q1 fiscal year 2025'])
def test_explicit_fiscal_quarter_localization_preserves_the_same_contextual_fact(source):
    assert not positional_numbers_conflict([f'<p>{source} revenue 11%, $18.5 billion.</p>'],
                                          ['<p>2025 财年第一季度，收入11%，185亿美元。</p>'])


@pytest.mark.parametrize('target', ['2025 财年第二季度', '2026 财年第一季度', '2025 年第一季度',
                                    '2025 财年第一季度 2025', '2025 财年第五季度'])
def test_contextual_fiscal_quarter_does_not_hide_wrong_or_added_numbers(target):
    assert positional_numbers_conflict(['<p>Q1 FY2025</p>'], [f'<p>{target}</p>'])
