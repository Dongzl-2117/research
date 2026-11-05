"""原创虚构资料。仅验收交互/回放，不代表真实库或模型效果。"""
from dataclasses import replace
from datetime import UTC, datetime

from researchlib.experiments.schema import FrozenCase, QuerySpec, Row, Snapshot
from researchlib.retrieval import Query, ScoreContext, Term
from researchlib.sources import Source


def demo_snapshot():
    moment = datetime(2026, 11, 1, tzinfo=UTC)
    sources = [Source("demo-a", "虚构研究札记", "王小明", "markdown", True),
               Source("demo-b", "虚构观察周报", "李四", "markdown", True)]
    data = [
        (1, "会员制 零售", "会员制让零售商可以以较低毛利销售商品。稳定的会员费支持低价策略。", "demo-a"),
        (2, "零售观察", "零售企业通过会员制收取年费，续费率和购物频次决定客户终身价值。", "demo-b"),
        (3, "现金流与股东回报", "现金流充裕时回购能减少流通股。应先保留经营资金再讨论回购。", "demo-a"),
        (4, "现金流 回购", "现金流并非总能用于回购，扩产和偿债也会消耗资金。", "demo-b"),
        (5, "收入与成本", "收入增长而利润下降，原因是新增渠道费用和原料成本上升。", "demo-a"),
        (6, "收入 利润", "收入下降但利润率提高，是因为低利润业务退出。", "demo-b"),
    ]
    rows = [Row(article_id=i, ord=0, title=title, body=body, source_id=source, published_at=moment)
            for i, title, body, source in data]
    cases = []
    for index, question in enumerate(("会员制 零售", "现金流 回购", "收入 利润"), 1):
        terms = [Term(word, "word", group, forms=[word], idf=1.5, df=2)
                 for group, word in enumerate(question.split())]
        query = Query(question, terms, [], [], [], [], [], False, False, False, False)
        context = ScoreContext(45, [], [], {}, as_of=moment)
        cases.append(FrozenCase(
            spec=QuerySpec(id=f"demo-{index}", query=question, scope={"source_ids": [s.id for s in sources]}),
            query=query, qa_query=replace(query), rows=rows, qa_rows=rows, context=context, qa_context=context,
        ))
    return Snapshot(material="synthetic", created_at=moment, code_revision="synthetic-fixture-v1",
                    sources=sources, cases=cases)
