from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel, RootModel

from models.meta import TableRoleOption, ColumnRoleOption
from plugins.agents.models import JSONOutputValidator, RerankModel


def strip_fence(text: str) -> str:
    """去掉模型偶尔附加的 Markdown 代码块围栏（```json ... ```）。

    只负责「文本 → 文本」的清洗，解析与校验交给各模型自己的
    validate_output：LLM 输出统一按 JSON 文本校验。
    """
    content = text.strip()
    if content.startswith("```"):
        content = content.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return content


class Mode(StrEnum):
    """检索模式。

    FULL（完整模式）：走完整流程，先做意图识别，SQL 校验时调用大模型判断查询是否合规；
    EVALUATION（评估模式）：跳过意图识别节点，且 SQL 校验只做 EXPLAIN 语法校验、不调用大模型。
    """
    FULL = "full"
    EVALUATION = "evaluation"


@dataclass()
class Context:
    """图运行上下文：由 StateGraph(context_schema=Context) 注入为 runtime.context。

    节点通过 runtime.context.model 取用 LLM，通过 runtime.context.embedding_model 取用嵌入模型，
    通过 runtime.context.rerank_model 取用重排模型（重排客户端本身不绑定模型，每次调用传入它）。

    检索哪个库、哪种模式属于这一轮的输入、随状态走（见 State.database_id / State.mode），
    不放上下文里——上下文只装那些与状态无关、每轮都一样的依赖。
    """
    model: BaseChatModel
    embedding_model: Embeddings
    rerank_model: RerankModel


@dataclass()
class State:
    """检索图的状态。

    question：输入，这一轮要转成 SQL 的自然语言问题，各节点都围着它干活。

    database_id：输入（必填），本次检索的业务库编号（Database.id）：字段与取值都在该库的向量集合里
        召回（见 Qdrant.find_columns / find_values），补充库信息、校验与执行 SQL 也连它，不跨库。

    mode：输入，检索模式（见 Mode）：整轮检索按它决定是否走意图识别与模型合规校验。

    其余字段是各节点跑出来的中间结果（关键词、召回的表 / 字段 / 指标 / 取值、生成的 SQL 与重试次数），
    由节点自己写入与取用。
    """
    question: str
    database_id: int
    mode: Mode = Mode.FULL
    keywords: list[str] | None = None
    error: str | None = None
    column_infos: dict[str, list[ColumnInfo]] | None = None
    metric_infos: list[MetricInfo] | None = None
    table_infos: dict[str, TableInfo] | None = None
    db_info: DatabaseInfo | None = None
    date_info: DateInfo | None = None
    sql: str | None = None
    retry_count: int = 0


class StepStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    ERROR = "error"


@dataclass()
class Step:
    """节点进度：逐条推给前端的流式消息。

    message：该节点正在做的工作，如「正在进行意图识别」，开始与结束时各上报一条；
    status：running / success / error；
    error：失败原因（status 为 error 时携带），如校验不通过的理由、SQL 为空、调用异常信息；
    sql：该节点产出的 SQL（生成、校正、执行成功时携带），供前端展示；
    data：查询结果等需要透出的数据（执行成功时携带）；
    table_infos：本次检索到的表信息（与 SQL 同一帧携带）：重排后最终用于生成这条 SQL 的表、字段与取值，
    评估模式在校验通过那一帧、完整模式在执行完成那一帧随 SQL 一起交出；评估侧据此把「检索到的片段」落库
    （见 models.dataset.Result.retrieved_contexts）。
    """
    message: str
    status: StepStatus
    error: str | None = None
    sql: str | None = None
    data: Any = None
    table_infos: list[TableInfo] | None = None


class Intention(JSONOutputValidator, BaseModel):
    """LLM 输出的意图判定结果：continued 表示问题是否适合生成查询，reason 为判定理由。

    校验模型输出时用 validate_output，例如：
    Intention.validate_output('{"continued": true, "reason": null}')。
    """

    continued: bool
    reason: str | None = None

    @classmethod
    def validate_output(cls, text: str) -> Intention:
        """清洗代码块围栏后校验模型输出，并返回意图判定结果。"""
        return cls.model_validate_json(strip_fence(text))


class SQLValidation(JSONOutputValidator, BaseModel):
    """LLM 输出的 SQL 合规校验结果：continued 表示查询是否放行，reason 为放行 / 拒绝的理由。

    校验模型输出时用 validate_output，例如：
    SQLValidation.validate_output('{"continued": false, "reason": "禁止写入"}')。
    """

    continued: bool
    reason: str | None = None

    @classmethod
    def validate_output(cls, text: str) -> SQLValidation:
        """清洗代码块围栏后校验模型输出，并返回合规校验结果。"""
        return cls.model_validate_json(strip_fence(text))


class KeywordList(JSONOutputValidator, RootModel[list[str]]):
    """LLM 输出的关键词字符串列表：根类型即 list[str]，校验的是「裸列表」本身。

    校验模型输出时用 validate_output，例如：
    KeywordList.validate_output('["下单时间", "订单金额"]')。
    """

    @classmethod
    def validate_output(cls, text: str) -> list[str]:
        """清洗代码块围栏后校验模型输出，并直接返回根列表。"""
        return cls.model_validate_json(strip_fence(text)).root


class FilteredTable(BaseModel):
    name: str
    columns: list[str]


class FilteredTableList(JSONOutputValidator, RootModel[list[FilteredTable]]):
    """LLM 输出的表过滤结果：表名与该表被保留的字段名列表。

    根类型即 list[FilteredTable]，校验的是「裸列表」本身，校验模型输出时用
    validate_output，例如：
    FilteredTableList.validate_output('[{"name": "orders", "columns": ["amount"]}]')。
    """

    @classmethod
    def validate_output(cls, text: str) -> list[FilteredTable]:
        """清洗代码块围栏后校验模型输出，并直接返回根列表。"""
        return cls.model_validate_json(strip_fence(text)).root


@dataclass()
class TableInfo:
    id: str
    name: str
    role: TableRoleOption
    description: str
    columns: list[ColumnInfo] | None = None


@dataclass()
class ColumnInfo:
    """检索阶段的字段信息。

    values：该字段在本次任务中可能的取值，由取值召回按 column_id 挂载；
    本次未召回到取值时为空列表。
    """
    id: str
    name: str
    type: str
    description: str
    alias: list[str]
    table_id: str
    values: list[Any] | None = None
    role: ColumnRoleOption | None = None


@dataclass()
class MetricInfo:
    """指标信息：id 取自元数据库的 Metric.id，即雪花 ID（整数）。"""
    id: int
    name: str
    description: str
    alias: list[str]


@dataclass()
class DatabaseInfo:
    dialect: str
    version: str
    charset: str


@dataclass()
class DateInfo:
    now: str
    week_day: int
    quarter: int
