from enum import StrEnum
from typing import Any

from tortoise.fields import BigIntField, CharField, TextField, JSONField, BooleanField, CharEnumField, \
    ForeignKeyField, ReverseRelation, RESTRICT
from models import Model
from plugins.snowflake import Snowflake


class DatabaseTypeOption(StrEnum):
    """业务库类型：连接与方言都按它走。当前只实现了 MySQL——取值查询、表结构读取与 EXPLAIN
    校验用的都是 MySQL 的 information_schema / 语法（见 models.get_table_infos / models.explain_sql），
    别的库型要先补齐这些再放开取值。
    """
    MYSQL = "mysql"


class Database(Model):
    """业务库注册表：一个可供采集与查询的数据库。

    id 用雪花 ID：全局唯一、趋势递增的有符号 64 位整数（见 plugins.snowflake），
    因此字段为 BigIntField 且 generated=False——不入库自增，取值在 Python 侧由 default 生成
    （Tortoise 构造实例时对缺省字段调用可调用的 default，save 与 bulk_create 都走这条路）。

    collected 表示「这个库已经整库采集过」：采集成功后由采集任务置位（见
    plugins.celery.tasks.collect_database），置位之后库的登记就不再允许修改——采集落下的表编号、
    字段与向量都挂在这条登记（尤其是连接地址）上，改了登记等于换了库。
    """
    id = BigIntField(pk=True, generated=False, default=Snowflake.get_id, description="数据库编号（雪花 ID）")
    name = CharField(max_length=128, description="数据库名称")
    url = CharField(max_length=255, description="数据库连接地址")
    type = CharEnumField(DatabaseTypeOption, default=DatabaseTypeOption.MYSQL, description="数据库类型")
    extra = JSONField[dict[str, Any] | None](null=True, description="附加配置（JSON）")
    collected = BooleanField(default=False, description="是否已采集")
    tables: ReverseRelation["Table"]

    class Meta:
        table = "database"


class TableRoleOption(StrEnum):
    FACT = "fact"
    DIM = "dim"


class Table(Model):
    """业务库里的表：归属于 Database 中的某个库。

    database 必填：表编号形如 <Database.id>.<表名>，库归属由采集图的上下文 database_id 提供
    （见 plugins.agents.modeling.schemas.Context）；已登记的表沿用元数据库里的原编号与原关联。
    """

    id = CharField(max_length=255, pk=True, description="表编号")
    name = CharField(max_length=128, null=True, description="表名称")
    role = CharField(max_length=100, null=True, description="表类型(fact/dim)")
    description = TextField(null=True, description="表描述")
    database = ForeignKeyField("models.Database", "tables", RESTRICT, description="所属数据库编号")
    database_id: int
    columns: ReverseRelation[Column]

    class Meta:
        table = "table"
        # 库内表名唯一（业务库本来就是这么保证的）：跨库同名表各有各的编号，
        # 元数据库层面也兜底，避免改名 / 并发采集 / 模型幻觉插出重复的 (数据库, 表名)
        unique_together = ("database_id", "name")


class ColumnRoleOption(StrEnum):
    PRIMARY_KEY = "primary_key"
    FOREIGN_KEY = "foreign_key"
    MEASURE = "measure"
    DIMENSION = "dimension"


class Column(Model):
    id = CharField(max_length=255, pk=True, description="列编号")
    name = CharField(max_length=128, null=True, description="列名称")
    type = CharField(max_length=64, null=True, description="数据类型")
    role = CharEnumField(ColumnRoleOption, null=True, description="列类型(primary_key,foreign_key,measure,dimension)")
    description = TextField(null=True, description="列描述")
    alias = JSONField[list[str] | None](null=True, description="列别名")
    table = ForeignKeyField("models.Table", "columns", RESTRICT, description="所属表编号")
    table_id: str
    synchronized = BooleanField(default=False, description="是否为枚举列")

    class Meta:
        table = "column"


class Metric(Model):
    """业务指标：id 用雪花 ID 生成（同 Database，见其说明）。"""

    id = BigIntField(pk=True, generated=False, default=Snowflake.get_id, description="指标编号（雪花 ID）")
    name = CharField(max_length=128, null=True, description="指标名称")
    description = TextField(null=True, description="指标描述")
    alias = JSONField[list[str] | None](null=True, description="指标别名")

    class Meta:
        table = "metric"
