from typing import Any

from pydantic import Field

from models.meta import ColumnRoleOption, DatabaseTypeOption, TableRoleOption
from schemas import Schema


class DatabaseCreate(Schema):
    """新增业务库的请求体：名称、连接地址、类型与附加配置。

    编号是雪花 ID、由服务端生成（见 models.meta.Database 的 default），不在请求体里；
    collected（是否已采集）也不在请求体里——它由采集流程写：整库采集成功才置位，登记时一律「未采集」。
    """
    name: str = Field(min_length=1, max_length=128)
    url: str = Field(min_length=1, max_length=255)
    type: DatabaseTypeOption = DatabaseTypeOption.MYSQL
    extra: dict[str, Any] | None = None


class DatabaseUpdate(Schema):
    """修改业务库的请求体：名称、连接地址、类型与附加配置。

    已采集的库不能改（见 controllers.modeling.update_database）：采集落下的表编号、字段、取值与向量
    都挂在这条登记（尤其是连接地址）上，改了登记等于换一个库；要改就先删掉这条登记重新采集。
    未提交的字段保持原值，显式提交 null 表示清空（extra 整组替换）。
    """
    name: str | None = Field(default=None, min_length=1, max_length=128)
    url: str | None = Field(default=None, min_length=1, max_length=255)
    type: DatabaseTypeOption | None = None
    extra: dict[str, Any] | None = None


class TableCreate(Schema):
    """新增表的请求体：表名、表类型、描述与所属数据库编号。

    表编号不在请求体里，由所属数据库编号与表名拼出（<database_id>.<表名>），故 database_id 必填，
    与采集表时的编号口径一致（见 plugins.agents.modeling.nodes.discover_tables）；描述可为空，
    表类型取 fact / dim 两种。
    """
    name: str = Field(min_length=1, max_length=128)
    role: TableRoleOption
    description: str | None = None
    database_id: int


class TableUpdate(Schema):
    """修改表的请求体：只接受表类型与描述。

    name 与 database_id 都不在可改字段里：两者一起决定表编号，表编号又被字段编号引用
    （<表编号>.<字段名>，见 models.meta.Column），改名等于换一张表，会牵动字段主键与向量库里的点；
    未提交的字段保持原值，role / description 显式提交 null 表示清空。
    """
    role: TableRoleOption | None = None
    description: str | None = None


class ColumnUpdate(Schema):
    """修改字段的请求体：只接受字段角色、描述与别名。

    name 与 table_id 一起决定字段编号（<表编号>.<字段名>，见 models.meta.Column），编号被向量库里的
    字段点引用，改名等于换一个字段，故不接受修改。

    type 与 synchronized 同样不在可改字段里，两者都是采集口径的登记而非人工填写的属性：type 取业务库的
    真实类型（采集时按业务库结构登记，见 plugins.agents.modeling.nodes.discover_tables），该不该采集取值
    也由类型决定（见 models.is_value_type）；synchronized 是「这个字段的取值要不要采集」的标记，由采集时
    的模型分析给出。改这两样等于手动偏离业务库现状，要改就重跑一轮采集。

    可改的三个：role 取 primary_key / foreign_key / measure / dimension、description 为字段描述、
    alias 为别名数组（整组替换，不再合并）。未提交的字段保持原值，显式提交 null 表示清空。
    """
    role: ColumnRoleOption | None = None
    description: str | None = None
    alias: list[str] | None = None


class MetricCreate(Schema):
    """新增指标的请求体：指标名、描述与别名。

    指标编号是雪花 ID、由服务端生成（见 models.meta.Metric 的 default），不在请求体里；
    指标不归属具体业务库（跨库使用），故也没有 database_id。
    """
    name: str = Field(min_length=1, max_length=128)
    description: str | None = None
    alias: list[str] | None = None


class MetricUpdate(Schema):
    """修改指标的请求体：指标名、描述与别名都在可改字段里。

    与表 / 字段不同，指标编号是雪花 ID、与名字无关，改名不影响任何引用，故允许改；
    未提交的字段保持原值，显式提交 null 表示清空。
    """
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = None
    alias: list[str] | None = None
