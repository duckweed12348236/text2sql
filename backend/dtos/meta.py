from pydantic import BaseModel, ConfigDict
from typing import Any, Literal


class ColumnInfo(BaseModel):
    id: str
    name: str
    type: str
    role: Literal['primary_key', 'foreign_key', 'dimension', 'measure']
    examples: list[Any]
    description: str
    alias: list[str]
    table_id: str
    model_config = ConfigDict(from_attributes=True)


class ColumnMetric(BaseModel):
    column_id: str
    metric_id: str
    model_config = ConfigDict(from_attributes=True)


class MetricInfo(BaseModel):
    id: str
    name: str
    description: str
    relevant_columns: list[str]
    alias: list[str]
    model_config = ConfigDict(from_attributes=True)


class TableInfo(BaseModel):
    id: str
    name: str
    role: Literal['dim', 'fact']
    description: str
    model_config = ConfigDict(from_attributes=True)


class ValueInfo(BaseModel):
    id: str
    value: str
    column_id: str
    model_config = ConfigDict(from_attributes=True)