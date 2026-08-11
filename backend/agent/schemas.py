from pydantic import BaseModel, ConfigDict
from typing import Annotated, Any
from langchain.messages import AnyMessage
from langgraph.graph import add_messages
from typing import Literal

from repositories.es import ESRepository
from repositories.mysql import DWDBRepository, MetaDBRepository
from repositories.qdrant import ColumnQdrantRepository, MetricQdrantRepository
from dtos.meta import ColumnInfo, MetricInfo, TableInfo
from dtos.es import ValueInfo


class WSAgentTableInfoState(BaseModel):
    id: str
    name: str
    role: Literal['dim', 'fact']
    description: str
    columns: list[ColumnInfo]


class WSAgentState(BaseModel):
    messages: Annotated[list[AnyMessage], add_messages]
    should_continue: bool | None = None
    guide_quries: list[str] | None = None
    keywords: list[str] | None = None
    recalled_columns: list[ColumnInfo] | None = None
    recalled_metrics: list[MetricInfo] | None = None
    recalled_values: list[ValueInfo] | None = None

    table_infos: list[WSAgentTableInfoState] | None = None
    metric_infos: list[MetricInfo] | None = None

    db_info: str | None = None
    date_info: str | None = None

    sql: str | None = None

    error: str | None = None


class WSAgentContext(BaseModel):
    column_qdrant_repo: ColumnQdrantRepository
    metric_qdrant_repo: MetricQdrantRepository
    es_repo: ESRepository
    meta_db_repo: MetaDBRepository
    dw_db_repo: DWDBRepository

    model_config = ConfigDict(arbitrary_types_allowed=True)


class WSStepInfo(BaseModel):
    step: str
    status: Literal['running', 'success', 'error']
    data: list[Any] | None = None
    finish: bool = False
    guide_queries: list[str] | None = None