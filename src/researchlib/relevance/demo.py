"""原创匿名合成任务，只演示流程；不包含期望标签，不用于证明泛化。"""

from researchlib.relevance.grouping import freeze
from researchlib.relevance.schema import Algorithm, Candidate, Manifest, Task
from researchlib.relevance.store import record


def demo_records():
    manifest = Manifest(dataset_id="synthetic-relevance-demo-v1", seed="fixed-demo-seed-v1")
    examples = [
        (
            "虚构零售商如何靠会员费盈利？",
            "entity:fictional-retailer",
            "topic:membership-economics",
            [
                (
                    "会员制度说明",
                    "虚构零售商向会员按年收取服务费，用稳定的续费收入覆盖部分固定开支，商品维持较低加价。",
                ),
                ("冷库维护记录", "该虚构商店的冷库周二完成设备保养。值班员更换温度传感器，核对了过去七天的制冷日志。"),
            ],
        ),
        (
            "虚构软件商的客户迁移为何困难？",
            "entity:fictional-software",
            "topic:migration-costs",
            [
                (
                    "接口与流程",
                    "客户的数据格式、内部审批规则与扩展接口紧密绑定。迁移需要逐个转换字段并重做工作流验证。",
                ),
                (
                    "办公室布局",
                    "软件商将会议室搬到南侧，新增两间隔音电话室。员工可以预约公共工位，午间休息区提供茶水。",
                ),
            ],
        ),
        (
            "虚构医院如何缩短候诊时间？",
            "entity:fictional-clinic",
            "topic:appointment-queues",
            [
                (
                    "分时预约",
                    "医院将原先集中到场改为分时预约，按检查时长保留弹性名额。复诊病人先在线填写资料，减少柜台停留。",
                ),
                (
                    "园区绿化",
                    "医院北门外种植了三排乔木，夏季为步道遮阴。园艺组每月检查灌溉管线，并修剪靠近围墙的枝条。",
                ),
            ],
        ),
    ]
    tasks = []
    for index, (query, entity, topic, passages) in enumerate(examples):
        candidates = []
        for offset, (title, text) in enumerate(passages):
            candidate = Candidate(
                source_id="synthetic-fixture",
                article_id=f"synthetic-{index}-{offset}",
                chunk_id="chunk-0",
                window_id="window-0",
                document_family_id=f"synthetic-family-{index}-{offset}",
                content_version="original-synthetic-v1",
                article_title=title,
                text=text,
                context_before="以下是为标注流程编写的虚构材料。",
                context_after="没有对应真实人物、公司或用户文档。",
                source_url="https://example.invalid/synthetic",
                algorithm=Algorithm(name="fixed-synthetic-fixture", version="1"),
            )
            candidate = Candidate.model_validate(
                candidate.model_dump() | {"source_url": f"/annotate/source/{candidate.candidate_id}"}
            )
            candidates.append(candidate)
        tasks.append(
            Task(
                query=query, entity_ids=[entity], topic_ids=[topic], candidates=tuple(candidates), material="synthetic"
            )
        )
    return [record("manifest", manifest)] + [record("task", task) for task in freeze([], tasks, manifest)]
