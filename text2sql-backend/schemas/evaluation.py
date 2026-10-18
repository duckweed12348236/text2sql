from pydantic import Field

from schemas import Schema


class DatasetCreate(Schema):
    """新建数据集的请求体：出题所用表、出题规模与所用业务库。

    table_ids 是出题所用的表（元数据库 Table.id，形如 <库编号>.<表名>）：表由调用方指定，评估图不再
    自己挑表（见 plugins.agents.evaluation.graph.Evaluator.invoke 的 table_ids），这些表连同字段与取值
    由评估图的节点按编号组装成库上下文（见 plugins.agents.evaluation.nodes.build_table_infos，
    落进 Dataset.reference_contexts）；一次最多 10 张——每张表的字段与取值都要进提示词，表越多上下文越大、
    出题越慢越容易跑到限流，故在上限处直接拦住；
    size 是生成的问题数量（出题、生成 SQL 与评测都是大模型请求，越大越慢）；
    database_id 是这些表所属的业务库（元数据库里的 Database.id），随初始状态带进评估图
    （见 plugins.agents.evaluation.schemas.State.database_id）。
    会话编号不在请求体里：由服务端生成（雪花 ID，见 plugins.snowflake），随新建数据集的响应返回
    （见 controllers.evaluation.create_dataset）。
    """
    table_ids: list[str] = Field(min_length=1, max_length=10)
    size: int = Field(ge=1)
    database_id: int = Field(ge=1)


class DatasetEvaluation(Schema):
    """评估数据集的请求体：只带本次评测的业务库编号。

    数据集按编号定位，会话编号取自 Dataset.thread_id，故请求体里不需要它；
    database_id 是检索智能体答题所在的库：字段与取值都在该库的向量集合里召回，随初始状态交给评估图
    （见 plugins.agents.evaluation.schemas.State.database_id）。同一个数据集可以换库反复评测，故它按请求走、
    不跟着出题那一轮定死。
    """
    dataset_id: int = Field(ge=1)
    database_id: int = Field(ge=1)
