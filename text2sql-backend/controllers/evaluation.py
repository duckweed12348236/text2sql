from fastapi import APIRouter, HTTPException, Query, status
from loguru import logger
from tortoise.transactions import in_transaction

from models.dataset import Dataset, DatasetItem, Result
from models.meta import Database, Table
from plugins.agents.evaluation.graph import Evaluator
from plugins.celery import submit_task
from plugins.redis import Task, TaskType
from plugins.snowflake import Snowflake
from schemas.evaluation import DatasetCreate, DatasetEvaluation

router = APIRouter(prefix="/evaluation")
# API 进程只拿它清会话记忆（删数据集时忘记那一轮，见 plugins.agents.evaluation.graph.Evaluator.forget）：
# 出题与评测都在 worker 进程里跑（见 plugins.celery.tasks），本进程不跑评估图
evaluator = Evaluator()
# 元数据库连接名（见 config.TORTOISE_ORM_CONFIG）：数据集、条目与评测结果都落在它上面，
# 一个请求里多步读写要在一个事务里完成，事务按连接名开（与 plugins.agents.evaluation.nodes 同名同值）
META_CONNECTION = "meta"


async def get_dataset(dataset_id: int) -> Dataset:
    """按数据集编号取数据集，取不到即 404（查看、删除与评估都以它为入口）。"""
    dataset = await Dataset.get_or_none(id=dataset_id)
    if dataset is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "指定的数据集不存在")
    return dataset


@router.get("/table")
async def list_tables(database_id: int = Query(ge=1), name: str | None = Query(default=None, max_length=128)):
    """列出某个业务库里已登记的表，供选表用；name 可按表名模糊搜索（不区分大小写的子串匹配）。

    新建数据集要在表里挑出题所用的表（见 create_dataset 的 table_ids），这些表与出题所用业务库同属一家
    （表编号形如 <库编号>.<表名>），故按库查才是可枚举的范围：database_id 指定业务库编号（元数据库里的
    Database.id），库没登记时报 404，与「库下没有表」的空列表区分开；每条的 id 就是出题 / 评测要回的
    Table.id，按表名排序，便于挑选。

    name 不传或只传空白视作不过滤；传了就按 LIKE '%name%' 匹配表名（Tortoise 的 icontains），
    故 % 与 _ 会被当作匹配符参与 LIKE，本接口只用于选表，不能拿它做唯一性判断。
    """
    if not await Database.filter(id=database_id).exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "指定的数据库不存在")
    query = Table.filter(database_id=database_id)
    keyword = (name or "").strip()
    if keyword:
        query = query.filter(name__icontains=keyword)
    tables = await query.order_by("name")
    return [x.dump() for x in tables]


@router.post("/dataset", status_code=status.HTTP_202_ACCEPTED)
async def create_dataset(dataset: DatasetCreate):
    """新建数据集：把出题任务交给后台的 Celery worker，立刻返回任务编号，进度靠轮询查（见 list_tasks）。

    一轮出题是一串大模型请求（生成问题与 SQL → 校验纠正 → 落库数据集），耗时以分钟计，故不在请求里
    跑完：这里只校验表、生成任务与会话编号，把任务记录写进 Redis（见 plugins.redis.Task：入参在
    meta.inputs、进度帧在 meta.steps），再把任务投进队列（任务见 plugins.celery.tasks.create_dataset），
    由 worker 执行——请求随即返回，进度由 worker 实时更新在任务记录上。

    表由调用方指定（一次最多 10 张，见 DatasetCreate.table_ids）：图不再自己挑表，worker 按编号取回表
    组装库上下文（见 plugins.agents.evaluation.nodes.build_table_infos）。校验在投递之前完成，
    故指定的表不存在时直接以 404 收场，不进队列。

    两个编号都由服务端生成（雪花 ID）：会话编号让出题跑到数据集落库后能接着恢复评测（见
    evaluate_dataset），它随响应返回、也记在数据集上（Dataset.thread_id）；任务编号用来轮询
    GET /evaluation/task，进度帧、落库的数据集编号与失败原因都在那条任务记录里。

    任务只投递、不等 worker：worker 没起来时任务就排在队列里，之后被取走照跑（见
    plugins.celery.CeleryWorker）；跑挂了会自动重试几次（见 plugins.celery.tasks.create_dataset）。
    任务记录最多留 3 天（见 plugins.redis.TASK_TTL），过期后轮询就查不到了。
    """
    tables = await Table.filter(id__in=dataset.table_ids).all().only("id")
    if len(tables) < len(set(dataset.table_ids)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "指定的表不存在")
    # 按编号排序：表的顺序固定下来，同一批表每次跑出来的上下文顺序才一致
    table_ids = sorted(table.id for table in tables)
    thread_id = Snowflake.get_id()
    task_id = await submit_task(TaskType.CREATE_DATASET,
                                {"thread_id": thread_id, "table_ids": table_ids, "size": dataset.size,
                                 "database_id": dataset.database_id},
                                "celery.create_dataset",
                                [thread_id, table_ids, dataset.size, dataset.database_id])
    logger.info(f"create_dataset: task {task_id}, size {dataset.size}, database {dataset.database_id}, "
                f"thread {thread_id}, {len(table_ids)} table(s) -> {table_ids}")
    return {"task_id": task_id, "thread_id": thread_id}


@router.get("/task")
async def list_tasks():
    """轮询任务进度：批量列出当前用户的任务记录（新的在前），一次能看多个任务。

    出题、评测与整库采集都是后台任务（见 create_dataset、evaluate_dataset 与 controllers.modeling.collect_database），
    调用方拿投递时返回的 task_id 在本接口返回的这一批里对号入座：任务按用户查（见 plugins.redis.Task.list，
    user_id 当前固定为 1——用户体系还没有），一次给出最近 plugins.redis.TASK_LIMIT 个，都在这一份列表里，
    不必逐个去问。

    每条是任务快照（见 plugins.redis.Task.dump）：编号、所属用户、状态、失败原因与 meta——
    meta.type 是任务类型（create_dataset 出题 / evaluate_dataset 评测 / collect_database 整库采集，
    见 plugins.redis.TaskType），meta.inputs 是投递时的入参，meta.steps 是到目前为止的进度帧，
    meta.outputs 是本轮产出（出题是 dataset_id，评测是 result_id 与 mean，采集是 database_id 与
    collected）。status 取 running / success / error——running
    表示还在跑（排队、执行中或重试中），另两个即本轮已经结束，不必自己去看最后一帧是 success 还是
    failed（进度帧的 status 用的是评估图那一套取值，任务的失败状态叫 error）；失败原因在 error 与
    最后一帧里。

    产出不必等到结束：meta.outputs.dataset_id 随出题落库那一刻就有了（据此即可去查看数据集）；评测的
    结果编号与平均分则在评测跑完、结果落库之后（见 plugins.celery.tasks.run_evaluation）。

    任务记录最多留 3 天（见 plugins.redis.TASK_TTL）：过期后不再出现在列表里——要重跑就另起一个任务。
    """
    tasks = await Task.list()
    return [task.dump() for task in tasks]


@router.get("/dataset")
async def list_datasets():
    """列出数据集：只有编号与名称，新的在前（编号是雪花 ID，递增）。

    会话编号、生成失败的原因与库上下文都不在列表里：会话编号是会话状态（checkpointer）的内部定位键，
    调用方按数据集编号走评估接口即可；error 是出题失败留下的诊断信息，库上下文又是体积很大的快照，
    两者都只在查看单条数据集时才有意义（见查看接口）。
    """
    datasets = await Dataset.all().only("id", "name").order_by("-id")
    return [x.dump() for x in datasets]


@router.get("/dataset/{dataset_id}")
async def get_dataset_detail(dataset_id: int):
    """查看数据集：数据集本体、条目（问题与参考 SQL）与历次评测结果。

    评测结果按编号倒序（新的在前），每条带上 ResultItem（参考答案、待评 SQL、得分与理由）；
    参考答案就是条目里那条参考 SQL（落库时直接抄过来的），对着它即知是哪条问题的判定。

    三次查询放在一个事务里：同一个数据集、它的条目与评测结果要取到同一时刻的一致快照，
    免得中途有评测落库，返回的条目数与结果对不上。
    """
    async with in_transaction(META_CONNECTION):
        dataset = await get_dataset(dataset_id)
        items = await DatasetItem.filter(dataset_id=dataset_id).order_by("id")
        results = await Result.filter(dataset_id=dataset_id).order_by("-id").prefetch_related("items")
    return {
        "dataset": dataset.dump(),
        "items": [item.dump() for item in items],
        "results": [result.dump() for result in results]
    }


@router.delete("/dataset/{dataset_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_dataset(dataset_id: int):
    """删除数据集：条目与历次评测结果随外键级联一起删（见 models.dataset 的 CASCADE）。

    同时清掉这一轮的会话记忆：会话状态按会话编号存在短期记忆组件里（PostgreSQL 里的 checkpoint
    与 Redis 里的缓存副本，见 Evaluator.forget），数据集删了它也再没人用得上，留着只是白占地方。
    取数与删除两步在一个事务里；记忆不参与这个事务，放在删行之后清——真出问题也只是多留一份没人
    访问的状态，不会出现「数据集还在、会话没了」。
    """
    async with in_transaction(META_CONNECTION):
        dataset = await get_dataset(dataset_id)
        await dataset.delete()
    await evaluator.forget(dataset.thread_id)
    logger.info(f"delete_dataset: dataset {dataset_id}, thread {dataset.thread_id}")


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def evaluate_dataset(evaluation: DatasetEvaluation):
    """评估数据集：把评测任务交给后台的 Celery worker，立刻返回任务编号，进度靠轮询查（见 list_tasks）。

    一轮评测是检索智能体的整轮答题（每条问题跑一遍检索图）加逐条判定与解读报告，耗时以分钟计，
    故与出题一样不在请求里跑完：这里只校验数据集、取它的会话编号，把任务记录写进 Redis
    （见 plugins.redis.Task：类型为 evaluate_dataset，入参在 meta.inputs），再把任务投进队列
    （任务见 plugins.celery.tasks.evaluate_dataset），由 worker 执行——请求随即返回，进度由 worker
    实时更新在任务记录上。

    database_id 是本次评测的业务库编号（见 DatasetEvaluation）：检索智能体在该库上答题。数据集按编号
    定位，会话编号取自 Dataset.thread_id，worker 按它恢复出题那一轮的状态——问题、参考 SQL 与出题所用的
    表都在里面，故请求里不必带表（见 Evaluator.invoke 的 table_ids）。会话状态由短期记忆组件保管
    （PostgreSQL 为准、Redis 做缓存，见 plugins.agents.checkpointers），应用重启后仍能接着恢复。

    同一个数据集可以反复评估：每投递一次就是一个独立的任务，各跑一轮、各落一条结果挂在数据集下
    （Dataset.results），进度写在各自的任务记录里。
    """
    dataset = await get_dataset(evaluation.dataset_id)
    task_id = await submit_task(TaskType.EVALUATE_DATASET,
                                {"dataset_id": evaluation.dataset_id, "thread_id": dataset.thread_id,
                                 "database_id": evaluation.database_id},
                                "celery.evaluate_dataset",
                                [evaluation.dataset_id, dataset.thread_id, evaluation.database_id])
    logger.info(f"evaluate_dataset: task {task_id}, dataset {evaluation.dataset_id}, "
                f"database {evaluation.database_id}, thread {dataset.thread_id}")
    return {"task_id": task_id}
