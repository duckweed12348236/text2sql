from pydantic import BaseModel
from dtos.meta import ColumnInfo, MetricInfo


class BaseQdrantInfo(BaseModel):
    id: str
    embeddings: list[float]
    payload: BaseModel


class ColumnQdrantInfo(BaseQdrantInfo):
    payload: ColumnInfo


class MetricQdrantInfo(BaseQdrantInfo):
    payload: MetricInfo