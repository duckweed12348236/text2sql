from __future__ import annotations
from typing import TYPE_CHECKING, Any
from dataclasses import dataclass
from enum import StrEnum

from langchain_openai.chat_models.base import BaseChatOpenAI
from pydantic import BaseModel, RootModel

from models.meta import TableRoleOption, ColumnRoleOption
from plugins.agents.models import JSONOutputValidator

if TYPE_CHECKING:
    from ragas.metrics.collections import SQLSemanticEquivalence

    from plugins.agents.retrieval.graph import Retriever


def strip_fence(text: str) -> str:
    """去掉模型偶尔附加的 Markdown 代码块围栏（```json ... ```）。

    只负责「文本 → 文本」的清洗，解析与校验交给各模型自己的
    model_validate_json：LLM 输出统一按 JSON 文本校验。
    """
    content = text.strip()
    if content.startswith("```"):
        content = content.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return content


class StringList(JSONOutputValidator, RootModel[list[str]]):
    """LLM 输出的字符串数组：根类型即 list[str]，校验的是「裸列表」本身。

    校验模型输出时用 validate_output，例如：
    StringList.validate_output('["t_order", "t_user"]')。
    """

    @classmethod
    def validate_output(cls, text: str) -> list[str]:
        """清洗代码块围栏后校验模型输出，并直接返回根列表。"""
        return cls.model_validate_json(strip_fence(text)).root


class SqlRecord(BaseModel):
    """单条 SQL 结果：问题标识 key（整数）与对应的 SQL。"""

    key: int
    sql: str


class SqlList(JSONOutputValidator, RootModel[list[SqlRecord]]):
    """LLM 输出的 SQL 列表：每项携带问题标识 key（整数）与对应 SQL。

    校验模型输出时用 validate_output，例如：
    SqlList.validate_output('[{"key": 1, "sql": "SELECT 1"}]')。
    """

    @classmethod
    def validate_output(cls, text: str) -> list[SqlRecord]:
        """清洗代码块围栏后校验模型输出，并直接返回根列表。"""
        return cls.model_validate_json(strip_fence(text)).root


class EvaluationReview(JSONOutputValidator):
    """LLM 输出的一轮评测解读报告（落进 Result.report）：整体结论、逐条归因、共性问题与改进建议。

    报告是 **纯文本**（Markdown 正文），模型直接输出正文、不做 JSON 包装，因此这里只把文本清洗一下，
    没有可校验失败的结构——配合 invoke 用时，重试只会发生在调用本身出错（限流、超时）的场合。
    """

    @classmethod
    def validate_output(cls, text: str) -> str:
        """返回报告正文：去掉首尾空白，以及模型偶尔自作主张加的代码块围栏。"""
        return strip_fence(text)


@dataclass()
class Context:
    """图运行上下文：由 StateGraph(context_schema=Context) 注入为 runtime.context。

    model：出题与生成 SQL 用的大模型；
    retriever：待评测的检索智能体，评测节点用它把问题跑成 SQL；
    metric：评测指标，当前为 Ragas 的 SQL 语义等效性（ragas.metrics.collections.SQLSemanticEquivalence）。

    只装这些与状态无关、每轮都一样的依赖：采哪个库属于这一轮的输入、随状态走（见 State.database_id），
    会话编号（雪花 ID）由节点从运行配置里取（runtime 本身不带 config，节点签名上添一个
    ``config: RunnableConfig`` 参数即可，见 plugins.agents.evaluation.nodes.correct_sql 与
    graph.Evaluator.invoke）——它本就是 langgraph 管的东西，不必在自己的上下文里再传一遍。
    """

    model: BaseChatOpenAI
    retriever: Retriever
    metric: SQLSemanticEquivalence


@dataclass()
class State:
    """评估图状态。table_ids 是出题所用的表编号（外部指定，见 graph.Evaluator.invoke 的 table_ids 入参）：
    图不自己挑表，表信息由节点按编号组装（见 nodes.build_table_infos）。

    database_id 是本次评测的业务库编号（Database.id）：检索智能体在该库上回答问题——字段与取值都在
    该库的向量集合里召回（见 plugins.agents.retrieval.schemas.State.database_id），校验与执行参考 SQL
    也连它。这里给了默认值 None 而不是必填：字段是后加进状态的，改造前落下的会话（checkpoint 里没有
    这个通道）遇到必填字段会连状态都装配不起来（langgraph 按 schema 装配状态），留个默认值才让它们
    还能打开；恢复这类会话时由 graph.Evaluator.invoke 用本次入参把编号补写进状态，故跑起来时它一定有值
    （节点里用 nodes.database_id_of 取，顺带兜住绕开 invoke 的调用）。

    table_infos 是出题节点组装出来的表快照（表描述、字段与取值）：纠正节点直接沿用这一份（不刷新），
    评测节点则在开跑前按 table_ids 重新取一遍再写回——出题到评测之间元数据可能被改过或重新采集过，
    而判定与解读都要看字段的业务含义，用最新的一份才判得准；纠正紧跟出题、其间元数据没机会变，
    多取一次只是白跑一趟。

    dataset_id 是纠正完成后落库的那条数据集编号（见 correct_sql 的落库一段）：
    评测结果要挂在它上面，故随状态一路带到尾节点。

    评测的明细不留在状态里：逐条判定与平均分在评测节点里直接落库（见 evaluate_dataset），
    状态只带到尾节点的编号与失败原因，避免大对象随每次状态更新复制一遍。
    """
    size: int
    table_ids: list[str]
    database_id: int | None = None
    table_infos: list[TableInfo] | None = None
    dataset_info: list[DatasetItemInfo] | None = None
    dataset_id: int | None = None
    error: str | None = None


@dataclass()
class TableInfo:
    id: str
    name: str
    role: TableRoleOption
    description: str
    columns: list[ColumnInfo]


@dataclass()
class ColumnInfo:
    id: str
    name: str
    type: str
    description: str
    alias: list[str]
    role: ColumnRoleOption
    values: list[Any]


@dataclass()
class DatasetItemInfo:
    question: str
    sql: str


# 评测的逐条结果用元组传递（检索生成的 SQL、等效性得分、得分理由、检索到的表信息，见 nodes.evaluate）：
# 它只服务于落库那一刻，不再单独建模，也不进图状态。


class StepStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


@dataclass()
class Step:
    """节点上报的进度帧：一条 message 配一个执行状态，失败时再附上 error。

    message：给用户看的进度文本；status：running / success / failed（见 StepStatus）；
    error：失败原因，仅 status 为 failed 时有值；
    dataset_id：本轮落库的数据集编号（Dataset.id）：出题阶段从数据集落库那一帧起带上，
    评测阶段的每一帧都带上——调用方据此把进度与数据集对上，不必等图跑完。
    """

    message: str
    status: StepStatus = StepStatus.RUNNING
    error: str | None = None
    dataset_id: int | None = None
