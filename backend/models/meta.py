from enum import StrEnum
from typing import Any

from tortoise.fields import CharField, TextField, JSONField, BooleanField, CharEnumField, ForeignKeyField, \
    ReverseRelation, RESTRICT
from models import Model


class TableRoleOption(StrEnum):
    FACT = "fact"
    DIM = "dim"


class Table(Model):
    id = CharField(max_length=64, pk=True, description="表编号")
    name = CharField(max_length=128, null=True, description="表名称")
    role = CharField(max_length=100, null=True, description="表类型(fact/dim)")
    description = TextField(null=True, description="表描述")
    columns: ReverseRelation[Column]

    class Meta:
        table = "table"


class ColumnRoleOption(StrEnum):
    PRIMARY_KEY = "primary_key"
    FOREIGN_KEY = "foreign_key"
    MEASURE = "measure"
    DIMENSION = "dimension"


class Column(Model):
    id = CharField(max_length=64, pk=True, description="列编号")
    name = CharField(max_length=128, null=True, description="列名称")
    type = CharField(max_length=64, null=True, description="数据类型")
    role = CharEnumField(ColumnRoleOption, null=True, description="列类型(primary_key,foreign_key,measure,dimension)")
    examples = JSONField[list[Any] | None](null=True, description="数据示例")
    description = TextField(null=True, description="列描述")
    alias = JSONField[list[str] | None](null=True, description="列别名")
    table = ForeignKeyField("models.Table", "columns", RESTRICT, description="所属表编号")
    table_id: str
    synchronized = BooleanField(default=True, description="是否同步")

    class Meta:
        table = "column"


class Metric(Model):
    id = CharField(max_length=64, pk=True, description="指标编码")
    name = CharField(max_length=128, null=True, description="指标名称")
    description = TextField(null=True, description="指标描述")
    relevant_columns = JSONField[list[str] | None](null=True, description="关联字段")
    alias = JSONField[list[str] | None](null=True, description="指标别名")

    class Meta:
        table = "metric"


class ColumnMetric(Model):
    column_id = CharField(max_length=64, null=False, description="列编号")
    metric_id = CharField(max_length=64, null=False, description="指标编号")

    class Meta:
        table = "column_metric"
        unique_together = (("column_id", "metric_id"),)
