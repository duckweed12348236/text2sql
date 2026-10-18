from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseChatModel
from pydantic import RootModel, model_validator

from models.meta import ColumnRoleOption, TableRoleOption
from schemas import Schema


def _pad(values: Any, size: int) -> list[Any]:
    """把位置数组补齐到固定长度（缺少的下标补 None）。"""
    values = list(values) if isinstance(values, (list, tuple)) else [values]
    return (values + [None] * size)[:size]


def _pick(source: dict[str, Any], *names: str) -> Any:
    """按候选字段名取值，用于兼容模型偶发的「对象」输出。"""
    for name in names:
        if name in source:
            return source[name]
    return None


class DiscoveredColumn(Schema):
    """大模型对单列的分析结果。

    正常输出是位置数组 [列名, 角色, 描述, 别名, 同步标记]；模型偶尔会改用字段名表示
    （如 {"name": ..., "role": ...}），这里一并兼容。
    """
    name: str
    role: ColumnRoleOption
    description: str = ""
    alias: list[str] = []
    synchronized: bool = False

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, value: Any) -> Any:
        if isinstance(value, (list, tuple)):
            name, role, description, alias, synchronized = _pad(value, 5)
            return {"name": name, "role": role, "description": description or "",
                    "alias": alias or [], "synchronized": bool(synchronized)}
        if isinstance(value, dict):
            return {"name": _pick(value, "name", "column_name", "column", "field"),
                    "role": _pick(value, "role", "column_role"),
                    "description": _pick(value, "description", "comment", "column_comment") or "",
                    "alias": _pick(value, "alias", "aliases") or [],
                    "synchronized": bool(_pick(value, "synchronized", "is_enum", "enum"))}
        return value


class DiscoveredTable(Schema):
    """大模型对单表的分析结果。

    正常输出是位置数组 [表名, 角色, 表描述, 列数组]；同样兼容字段名（对象）表示。
    """
    name: str
    role: TableRoleOption
    description: str = ""
    columns: list[DiscoveredColumn] = []

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, value: Any) -> Any:
        if isinstance(value, (list, tuple)):
            name, role, description, columns = _pad(value, 4)
            return {"name": name, "role": role,
                    "description": description or "", "columns": columns or []}
        if isinstance(value, dict):
            columns = _pick(value, "columns", "column_infos", "fields")
            if isinstance(columns, dict):
                columns = list(columns.values())
            return {"name": _pick(value, "name", "table_name", "table"),
                    "role": _pick(value, "role", "table_role"),
                    "description": _pick(value, "description", "comment", "table_comment") or "",
                    "columns": columns or []}
        return value


class DiscoveredTableList(RootModel[list[DiscoveredTable]]):
    """大模型输出的表分析结果：根类型即 list[DiscoveredTable]，校验的是「裸列表」本身。

    与检索侧 KeywordList / FilteredTableList 同一写法：
    - model_validate([element])：收原始列表，用于整体解析失败时逐个元素抢救（见 nodes._salvage_tables）；
    - model_validate_json(content)：收模型返回的 JSON 文本，用于整体解析（见 nodes.analyze），
      解析失败抛 ValidationError，调用方据此区分截断与格式错误。
    """


@dataclass()
class Context:
    """图运行上下文：由 StateGraph(context_schema=Context) 注入为 runtime.context。

    LLM 与嵌入模型都从这里传入：节点通过 runtime.context.model 取用 LLM，
    通过 runtime.context.embedding_model 取用嵌入模型。

    采哪个库属于这一轮的输入、随状态走（见 State.database_id），不放上下文里——
    上下文只装那些与状态无关、每轮都一样的依赖。
    """
    model: BaseChatModel
    embedding_model: Embeddings


@dataclass()
class State:
    """采集图的状态。

    table_ids：输入，指定本轮采集哪些表，采集与向量化都以它为准。这里给的是元数据库里的**表编号**
        （形如 <Database.id>.<表名>），不是业务库里的表名：表名只在库内唯一、跨库可以重名，
        而表编号全局唯一，按编号取表就不会跨库串台。
        已登记的编号用元数据库里的表名与库归属；未登记的编号按首次采集处理，从其前缀里取出表名。
        留空表示不限定范围：采集本库（库编号见 database_id）的全部表。
        库编号对不上或既没登记又不是 <database_id>.<表名> 形态的编号，会逐条上报采集失败。

    database_id：输入，本次采集的业务库编号（Database.id）：表编号以它作前缀，Table.database_id
        写它，不限定范围时也用它圈定范围；字段与取值还按它分开存进向量库（见 Qdrant 的
        columns.<数据库编号> / values.<数据库编号> 集合）。Table.database_id 是必填列、表编号也要靠它拼，
        故采集（discover_tables）与向量化（embed_columns）都必须带上；只有单独调 embed_columns、
        且 table_ids 已经指明了表时才能留空。
    """
    table_ids: list[str]
    database_id: int | None = None


class StepStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


@dataclass()
class Step:
    message: str
    status: StepStatus = StepStatus.RUNNING
    error: str | None = None
