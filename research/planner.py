"""把已解析的问题变成有上限、可校验依赖的研究任务。"""

from __future__ import annotations

from dataclasses import dataclass

from search.query_plan import QueryPlan


@dataclass
class ResearchTask:
    task_id: str
    task_type: str
    entity_ids: tuple[str, ...]
    depends_on: tuple[str, ...]
    required_slots: tuple[str, ...]
    allow_partial: bool
    objective: str


def build_tasks(plan: QueryPlan) -> list[ResearchTask]:
    entities = list(plan.entities)
    if plan.task_type == "company_statement" and entities:
        evidence = []
        for slot in ("profit", "balance_cashflow", "notes"):
            evidence.append(ResearchTask(
                f"{slot}:{entities[0].entity_id}",
                "company_evidence",
                (entities[0].entity_id,),
                (),
                (slot,),
                True,
                slot,
            ))
        return [
            *evidence,
            ResearchTask(
                "statement_bridge",
                "statement_bridge",
                (entities[0].entity_id,),
                tuple(task.task_id for task in evidence),
                ("profit", "cashflow"),
                False,
                "只在利润、现金流和附注证据齐备后解释差异",
            ),
        ]
    if plan.task_type == "industry":
        return [ResearchTask(
            "industry-scope",
            "industry_scope",
            (),
            (),
            ("coverage",),
            False,
            "冻结已收录样本并声明覆盖未知",
        )]
    tasks = [
        ResearchTask(
            f"company:{entity.entity_id}",
            "company_evidence",
            (entity.entity_id,),
            (),
            ("metrics",),
            True,
            entity.canonical_name,
        )
        for entity in entities
    ]
    if plan.route == "research" and len(tasks) >= 2:
        tasks.append(ResearchTask(
            "align",
            "align",
            tuple(entity.entity_id for entity in entities),
            tuple(task.task_id for task in tasks),
            ("comparison",),
            True,
            "对齐已取得的双方证据",
        ))
    if not tasks:
        tasks.append(ResearchTask("scope", "knowledge", (), (), (), True, "检索当前知识库"))
    return tasks


def validate_tasks(tasks: list[ResearchTask]) -> dict[str, list[str]]:
    errors: list[str] = []
    if len(tasks) > 6:
        errors.append("子任务超过 6 个")
    identifiers = [task.task_id for task in tasks]
    if len(identifiers) != len(set(identifiers)):
        errors.append("任务编号重复")
    dependencies = {task.task_id: task.depends_on for task in tasks}
    visiting: set[str] = set()
    visited: set[str] = set()

    def cyclic(task_id: str) -> bool:
        if task_id in visiting:
            return True
        if task_id in visited:
            return False
        visiting.add(task_id)
        for dependency in dependencies.get(task_id, ()):
            if cyclic(dependency):
                return True
        visiting.remove(task_id)
        visited.add(task_id)
        return False

    if any(cyclic(task_id) for task_id in dependencies):
        errors.append("任务存在循环依赖")
    return {"errors": errors}
