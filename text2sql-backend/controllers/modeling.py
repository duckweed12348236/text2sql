from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from loguru import logger
from tortoise.transactions import in_transaction

from models import Model, discard_connection, register_connection
from models.meta import Column, Database, Metric, Table
from plugins.agents.models import bge_large_zh_embeddings
from plugins.celery import submit_task
from plugins.qdrant import Qdrant
from plugins.redis import TaskType
from schemas import Schema
from schemas.modeling import ColumnUpdate, DatabaseCreate, DatabaseUpdate, MetricCreate, MetricUpdate, \
    TableUpdate

router = APIRouter(prefix="/modeling")
# 向量库封装：与采集图 / 检索图共用同一个实例（Qdrant 是插件单例，见 plugins.qdrant），
# 改字段时把改动同步过去（见 update_column）；嵌入模型取自 plugins.agents.models，与采集、检索同源
qdrant = Qdrant()
# 元数据库连接名（见 config.TORTOISE_ORM_CONFIG）：删库时表与字段的级联清理放在一个事务里完成
META_CONNECTION = "meta"


async def get_or_404(model: type[Model], object_id: int | str, message: str) -> Model:
    """按主键取一条元数据，取不到即 404（各资源的 get_* 都走它，报错口径统一）。"""
    instance = await model.get_or_none(id=object_id)
    if instance is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, message)
    return instance


async def get_database(database_id: int) -> Database:
    """按库编号取业务库，取不到即 404（改、删、采集都以它为入口）。"""
    return await get_or_404(Database, database_id, "指定的数据库不存在")


def database_dump(database: Database) -> dict[str, Any]:
    """业务库快照：连接地址（url）里含库账号口令，不进响应，其余字段照常返回。"""
    return {key: value for key, value in database.dump().items() if key != "url"}


async def get_table(table_id: str) -> Table:
    """按表编号取表，取不到即 404（改表与查字段列表都以它为入口）。"""
    return await get_or_404(Table, table_id, "指定的表不存在")


async def get_column(column_id: str) -> Column:
    """按字段编号取字段，取不到即 404。"""
    return await get_or_404(Column, column_id, "指定的字段不存在")


async def get_metric(metric_id: int) -> Metric:
    """按指标编号取指标，取不到即 404（改、删都以它为入口）。"""
    return await get_or_404(Metric, metric_id, "指定的指标不存在")


def apply_updates(instance: Model, payload: Schema) -> list[str]:
    """把请求体里显式提交的字段写进实例，返回需要落库的字段名；一个字段都没提交即 422。

    exclude_unset 区分「没提交」与「提交了 null」：前者不动、后者清空；请求体的字段名与模型字段
    一一对应（见 schemas.modeling），交 update_from_dict 写入并做类型转换（枚举、JSON 数组、null）。
    """
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "没有可修改的字段")
    instance.update_from_dict(updates)
    return list(updates)


@router.get("/database")
async def list_databases():
    """列出元数据库中已登记的业务库，按编号倒序（新登记的在前）。

    供前端选库用（表 / 字段 / 指标都按业务库划分），也用于展示采集状态：collected 为真的库已整库
    采集过、登记已锁死不能再改（见 update_database）。连接地址不进响应（里面含库账号口令），
    故这里用 only 只取要展示的几列，查询与返回都不带 url。
    """
    databases = await Database.all().only("id", "name", "type", "extra", "collected").order_by("-id")
    return [database_dump(x) for x in databases]


@router.post("/database")
async def create_database(database: DatabaseCreate):
    """新增业务库：登记名称、连接地址、类型与附加配置，登记完即可发起采集。

    编号是雪花 ID、由服务端生成；collected 一律从「未采集」开始——整库采集成功后才由采集任务置位
    （见 plugins.celery.tasks.collect_database），故请求体里没有这个字段。

    登记完顺手把连接注册进 Tortoise（见 models.register_connection）：连接按别名惰性创建，
    注册之后本进程里立刻能按编号取到该库的连接（检索、采集都用它），不必等重启。
    """
    instance = Database(**database.model_dump())
    await instance.save()
    await register_connection(instance.id, instance.url)
    logger.info(f"create_database: database {instance.id}（{instance.name}）")
    return database_dump(instance)


@router.put("/database/{database_id}")
async def update_database(database_id: int, database: DatabaseUpdate):
    """修改业务库：名称、连接地址、类型与附加配置；已采集的库不能再改。

    采集落下的表编号、字段、取值与向量都挂在这条登记（尤其是连接地址）上，采集之后再改登记等于
    换一个库，故一律拒绝（409）——包括改名：名字虽不进编号与连接，但登记已经锁死，口径统一；
    要改就先删掉这条登记、重新登记再采集。未提交的字段保持原值，显式提交 null 则清空该字段
    （extra 整组替换）。

    连接地址变了要同步连接（见 models.register_connection）：连接对象按地址惰性创建，旧的连接对象
    不丢，下一次取连接还会连到旧地址；类型不影响连接，改它不必重连。
    """
    instance = await get_database(database_id)
    if instance.collected:
        raise HTTPException(status.HTTP_409_CONFLICT, "该数据库已采集，不能再修改")
    updates = apply_updates(instance, database)
    await instance.save(update_fields=updates)
    if "url" in updates:
        await register_connection(instance.id, instance.url)
    logger.info(f"update_database: database {instance.id}（{instance.name}）改了 {list(updates)}")
    return database_dump(instance)


@router.delete("/database/{database_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_database(database_id: int):
    """删除业务库：连同它名下已登记的表与字段一起删，向量库里该库的两个集合也一并清掉。

    表与字段挂在库上（外键为 RESTRICT），得先删字段再删表才能删库，三步放在一个事务里完成；
    采集标记（collected）不影响删除——已采集的库删掉的是这条登记与采集产物，登记没了自然不能再采集，
    也没法再被检索（字段点随集合一起删）。

    向量库的清理放在事务之后（见 Qdrant.delete_database）：元数据库是登记的事实来源，先删登记；
    向量库清不动（Qdrant 不可用）只记日志、不影响删除结果——编号是雪花 ID、不会复用，留下的孤儿集合
    只是占空间。连接一并摘掉（见 models.discard_connection）。
    """
    await get_database(database_id)
    async with in_transaction(META_CONNECTION):
        await Column.filter(table_id__startswith=f"{database_id}.").delete()
        await Table.filter(database_id=database_id).delete()
        await Database.filter(id=database_id).delete()
    await discard_connection(database_id)
    try:
        await qdrant.delete_database(database_id)
    except Exception as error:  # noqa: BLE001 - 登记已删干净，向量库清不动只记日志
        logger.warning(f"delete_database: 清理业务库 {database_id} 的向量集合失败：{error}")
    logger.info(f"delete_database: database {database_id} deleted")


@router.post("/database/{database_id}/collect", status_code=status.HTTP_202_ACCEPTED)
async def collect_database(database_id: int):
    """发起整库采集：把采集任务交给后台的 Celery worker，立刻返回任务编号，进度靠轮询查。

    采集整库进行（登记表结构 → 分析字段 → 向量化 → 采集取值），一轮以分钟计，故不在请求里跑完：
    这里只校验库、写好任务记录（类型为 collect_database，入参在 meta.inputs），再把任务投进队列
    （任务见 plugins.celery.tasks.collect_database），由 worker 执行——请求随即返回，进度由 worker
    实时更新在任务记录上，轮询「评测数据集」章节的任务接口（GET /evaluation/task）即可看到；
    采集成功后库的登记就此锁死（collected 置位，见 update_database），worker 是采集成功才置位的。

    已采集的库可以再采：业务库结构变了、或上次采集有失败帧，重跑一轮覆盖即可。
    """
    database = await get_database(database_id)
    task_id = await submit_task(TaskType.COLLECT_DATABASE, {"database_id": database.id},
                                "celery.collect_database", [database.id])
    logger.info(f"collect_database: task {task_id}, database {database.id}（{database.name}）")
    return {"task_id": task_id}


@router.get("/table")
async def list_tables(database_id: int = Query(ge=1), name: str | None = Query(default=None, max_length=128)):
    """列出某个业务库里已登记的表，name 可按表名模糊搜索（不区分大小写的子串匹配）。

    database_id 指定业务库编号（元数据库里的 Database.id）：表都归属某个库（Table.database_id），
    按库查才是可枚举的范围。库没登记时报 404，与「库下没有表」的空列表区分开。

    name 不传或只传空白视作不过滤；传了就按 LIKE '%name%' 匹配表名（Tortoise 的 icontains），
    故 % 与 _ 会被当作匹配符参与 LIKE，本接口只用于选表，不能拿它做唯一性判断。
    结果按表名排序，便于挑选。
    """
    if not await Database.filter(id=database_id).exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "指定的数据库不存在")
    query = Table.filter(database_id=database_id)
    keyword = (name or "").strip()
    if keyword:
        query = query.filter(name__icontains=keyword)
    tables = await query.order_by("name")
    return [x.dump() for x in tables]


@router.put("/table/{table_id}")
async def update_table(table_id: str, table: TableUpdate):
    """修改表：只接受表类型与描述。

    表名与所属数据库编号一起决定表编号、且被字段编号引用（<表编号>.<字段名>），故不接受修改；
    未提交的字段保持原值，显式提交 null 则清空该字段。

    不需要同步向量库：向量库里只有字段（columns.<库编号>）与取值（values.<库编号>）两类点，
    表本身不参与向量化；检索用到的表信息是按字段载荷里的 table_id 回元数据库取的
    （见 plugins.agents.retrieval.nodes.recall_columns），故这里改完，检索下一次就能读到新类型与描述。
    """
    instance = await get_table(table_id)
    await instance.save(update_fields=apply_updates(instance, table))
    return instance.dump()


@router.get("/column")
async def list_columns(table_id: str = Query(min_length=1)):
    """列出某张表下已登记的字段。

    table_id 指定表编号（元数据库里的 Table.id，形如 <database_id>.<表名>）：字段都挂在表上
    （Column.table_id），按表查才是可枚举的范围。表没登记时报 404，与「表下没有字段」的空列表区分开。
    """
    await get_table(table_id)
    columns = await Column.filter(table_id=table_id).all()
    return [x.dump() for x in columns]


@router.put("/column/{column_id}")
async def update_column(column_id: str, column: ColumnUpdate):
    """修改字段：只接受字段角色、描述与别名（见 ColumnUpdate），改动一并同步到向量库。

    字段名与所属表一起决定字段编号（<表编号>.<字段名>）、编号又被向量库里的字段点引用，故不接受修改；
    类型与同步标记是采集口径的登记（业务库的真实类型、取值要不要采集），也不在这里改，要改就重跑采集。
    别名按整组替换（提交什么就是什么，不与原值合并）；未提交的字段保持原值，显式提交 null 则清空该字段。

    两步走：先落元数据库（本接口的登记），再按改动同步向量库（见 Qdrant.update_column）——
    描述与别名参与向量化（与字段名各成一个点，见 plugins.qdrant.VECTORIZED_FIELDS），改了就整批删掉旧点、
    重新向量化；只有角色变时只覆盖点的载荷，不必再调嵌入模型。
    向量库是派生数据（改字段走的就是采集图那套口径，见 plugins.agents.modeling.nodes.embed_columns）：
    同步失败时元数据库的改动已经落库、仍然生效，接口照常报错，重跑一轮采集即可补齐。
    """
    instance = await get_column(column_id)
    # 字段改动要按库写进对应的集合，库编号取自所属表（表编号形如 <库编号>.<表名>，见 models.meta.Table）
    table = await get_table(instance.table_id)
    updates = apply_updates(instance, column)
    await instance.save(update_fields=updates)
    await qdrant.update_column(table.database_id, instance, set(updates), bge_large_zh_embeddings)
    return instance.dump()


@router.get("/metric")
async def list_metrics():
    """列出元数据库中已登记的指标。"""
    metrics = await Metric.all()
    return [x.dump() for x in metrics]


@router.post("/metric")
async def create_metric(metric: MetricCreate):
    """新增指标：提交指标名、描述与别名。

    指标不归属具体业务库（跨库使用，见 Qdrant.register 的 metrics 集合），编号为雪花 ID、由服务端
    生成。这里只登记元数据库：指标还要向量化写进向量库的 metrics 集合才会被检索召回，而当前 Qdrant
    封装只提供了 find_metrics，还没有对应的写入方法（补上之前，新登记的指标召回不到）。
    """
    instance = Metric(**metric.model_dump())
    await instance.save()
    return instance.dump()


@router.put("/metric/{metric_id}")
async def update_metric(metric_id: int, metric: MetricUpdate):
    """修改指标：指标名、描述与别名都可改。

    与表 / 字段不同，指标编号是雪花 ID、与名字无关，改名不牵动任何引用；别名按整组替换。
    未提交的字段保持原值，显式提交 null 则清空该字段。
    """
    instance = await get_metric(metric_id)
    await instance.save(update_fields=apply_updates(instance, metric))
    return instance.dump()


@router.delete("/metric/{metric_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_metric(metric_id: int):
    """删除指标。

    只清元数据库：向量库 metrics 集合里的指标点不会随之清掉（Qdrant 封装没有删除接口），
    而检索是按向量库的点还原指标的（见 plugins.agents.retrieval.nodes.recall_metrics），
    故删掉的指标仍可能被召回；要清干净需自行按 payload 的 id 从该集合删点。
    """
    instance = await get_metric(metric_id)
    await instance.delete()
