from pydantic import BaseModel, ConfigDict
from typing import Literal


class ColumnConfig(BaseModel):
    name: str
    role: Literal['primary_key', 'foreign_key', 'dimension', 'measure']
    description: str
    alias: list[str]
    sync: bool
    model_config = ConfigDict(from_attributes=True)


class TableConfig(BaseModel):
    name: str
    role: Literal['dim', 'fact']
    description: str
    columns: list[ColumnConfig]


class MetricConfig(BaseModel):
    name: str
    description: str
    relevant_columns: list[str]
    alias: list[str]


class MetaConfig(BaseModel):
    tables: list[TableConfig]
    metrics: list[MetricConfig]