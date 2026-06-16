from enum import StrEnum
from typing import Annotated, Any

from langgraph.graph import add_messages
from langchain.messages import AnyMessage

from models.meta import TableRoleOption, ColumnRoleOption
from schemas import Schema


class State(Schema):
    messages: Annotated[list[AnyMessage], add_messages]
    guide: list[str] = []
    keywords: list[str] = []
    error: str | None = None
    columns: list[ColumnInfo] = []
    metrics: list[MetricInfo] = []
    values: list[ValueInfo] = []
    tables: list[TableInfo] = []
    db_info: dict[str, Any] = {}
    date_info: dict[str, Any] = {}
    sql: str | None = None
    retry_count: int = 0


class StepStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    ERROR = "error"


class Step(Schema):
    step: str
    guide_questions: list[str] = []
    status: StepStatus
    finished: bool = False
    data: Any = None


class Intention(Schema):
    continued: bool
    reason: str | None = None
    guide_questions: list[str] = []


class TableInfo(Schema):
    id: str
    name: str
    role: TableRoleOption
    description: str
    columns: list[ColumnInfo]


class ColumnInfo(Schema):
    id: str
    name: str
    type: str
    role: ColumnRoleOption | None = None
    examples: list[Any]
    description: str
    alias: list[str]
    table_id: str


class MetricInfo(Schema):
    id: str
    name: str
    description: str
    relevant_columns: list[str]
    alias: list[str]


class ValueInfo(Schema):
    id: str | int
    payload: str
    column_id: str
