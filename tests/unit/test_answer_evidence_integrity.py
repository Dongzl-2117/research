"""数字、出处编号和流式终态使用合成资料；不请求模型。"""

import pytest

from researchlib.citecheck import check_answer
from researchlib.llm import LLMError, _read_stream
from researchlib.retrieval import cited_numbers
from researchlib.snippets import answer_html


@pytest.mark.parametrize(('claim', 'source'), [
    ('10%', '10.5%'), ('10.5%', '10%'), ('15%', '15x'), ('15x', '15%'),
    ('-15%', '15%'), ('15%', '-15%'), ('+15%', '-15%'), ('15bp', '15%'),
])
def test_numeric_prefix_units_and_signs_cannot_fake_source_support(claim, source):
    flags = check_answer(f'合成指标为{claim}[1]。', [{'n': 1, 'text': f'合成指标为{source}。'}], [])
    assert any('数字' in item for row in flags for item in row['problems'])


@pytest.mark.parametrize(('claim', 'source'), [('10.5%', '10.5%'), ('15x', '15x'),
                                             ('-15%', '−15%'), ('2,300', '2300'), ('15%', '１５％')])
def test_faithful_numeric_tokens_remain_supported(claim, source):
    assert check_answer(f'合成指标为{claim}[1]。', [{'n': 1, 'text': f'合成指标为{source}。'}], []) == []


def test_four_digit_reference_is_checked_and_rendered():
    answer = '合成指标为10%[1000]。'
    assert cited_numbers(answer, 1000) == [1000]
    assert 'href="#c-1000"' in str(answer_html(answer))
    flags = check_answer(answer, [{'n': 1000, 'text': '合成指标为20%。'}], [])
    assert flags and flags[0]['refs'] == [1000]
    missing = check_answer(answer, [], [])
    assert any('不存在' in text for row in missing for text in row['problems'])


@pytest.mark.parametrize('tail', [
    [], ['data: [DONE]'],
    ['data: {"choices":[{"delta":{},"finish_reason":"stop"}]}'],
    ['data: {"error":{"message":"synthetic error"}}', 'data: [DONE]'],
    ['data: {"choices":[{"delta":{},"finish_reason":"length"}]}', 'data: [DONE]'],
    ['data: invalid-json', 'data: [DONE]'],
])
def test_partial_or_error_stream_is_not_success(tail):
    with pytest.raises(LLMError):
        _read_stream(['data: {"choices":[{"delta":{"content":"部分回答"}}]}', *tail])


def test_complete_primary_stream_keeps_usage_and_ignores_another_choice():
    body = _read_stream([
        'data: {"model":"synthetic","choices":[{"index":0,"delta":{"content":"完整回答"}},'
        '{"index":1,"delta":{"content":"另一候选"}}]}',
        'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}',
        'data: {"choices":[],"usage":{"total_tokens":5}}', 'data: [DONE]',
    ])
    assert body['choices'][0]['message']['content'] == '完整回答'
    assert body['choices'][0]['finish_reason'] == 'stop'
    assert body['usage']['total_tokens'] == 5
