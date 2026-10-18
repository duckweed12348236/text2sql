"""Celery 任务定义。

由 :data:`plugins.celery.celery_client` 的 ``include`` 引入，worker 启动时加载。任务跑在
worker 子进程里（不是 API 进程）：celery 的任务是同步的，要访问异步依赖（数据库、向量库、
模型）就用 :func:`plugins.celery.run` 把协程丢进 worker 常驻的事件循环——worker 启动时已经
注册好插件（见 ``worker_process_init`` 信号），连接可以直接复用。

后台任务（``celery.collect_database`` 整库采集、``celery.create_dataset`` 出题、
``celery.evaluate_dataset`` 评测）由 API 投递（见 ``controllers.modeling`` 与 ``controllers.evaluation``）：
进度实时写进 Redis 的任务记录（见 :class:`plugins.redis.Task`），调用方轮询任务接口拿
（见 ``controllers.evaluation.list_tasks``）——不占 Celery 的结果后端，也不必把进度塞进任务状态。
定时任务（``celery.scan_tables``）由 beat 按 ``config.CELERY_SCAN_INTERVAL`` 周期投递，
调度表挂在应用上（见 :data:`plugins.celery.celery_client`）：它比对业务库与元数据库的登记，
已移除的表清掉向量与登记，新增 / 变动的表交给建模智能体重采（见 :func:`scan_tables`）。
"""

from dataclasses import asdict
from typing import Any, AsyncGenerator, Coroutine

from celery.app.task import Task
from loguru import logger
from tortoise.transactions import in_transaction

from config import CELERY_BROKER_DB
from models import get_table_infos, get_table_names, register_connection
from models.dataset import Result
from models.meta import Column, ColumnRoleOption, Database, Table
from plugins.agents.evaluation.graph import Evaluator
from plugins.agents.evaluation.schemas import Step, StepStatus
from plugins.agents.modeling.graph import Modeler
from plugins.agents.modeling.schemas import StepStatus as ModelingStepStatus
from plugins.celery import celery_client, run
from plugins.qdrant import Qdrant
from plugins.redis import Task as TaskRecord, TaskStatus, TaskType, lock

# 评估图的入口：worker 进程级共用一个，worker 启动时就初始化好（见 plugins.celery.bootstrap_worker）。
# 与 API 进程里 controllers.evaluation.evaluator 是各自进程各一份的单例
evaluator = Evaluator()
# 采集图的入口：图本身没有重活要初始化（连接、向量库都是节点里现取的），进程级共用一个即可
modeler = Modeler()
# 向量库封装：与采集图 / 检索图共用同一个实例（Qdrant 是插件单例，见 plugins.qdrant），
# 巡检里清已移除表的字段点与取值点要用它（见 purge_tables）
qdrant = Qdrant()

# 巡检互斥锁（见 plugins.redis.lock）：一轮同时只跑一个。锁落在 broker 的库上，
# TTL 是兜底——持有者被强杀时不会有人释放锁，靠它自己过期，免得后续巡检一直跳过
SCAN_LOCK_KEY = "celery:scan_tables:lock"
SCAN_LOCK_TTL = 3600
# 元数据库连接名（见 config.TORTOISE_ORM_CONFIG）：清掉已移除表的登记时，字段与表放在一个事务里删
META_CONNECTION = "meta"

# 后台任务的重试次数与间隔（秒）：worker 出了意外（连不上库、进程被重启）时等一会儿重跑，
# 任务本身的业务失败（表已不存在、SQL 生成失败）不重试——同样的输入重跑还是同样的结果
TASK_RETRIES = 3
TASK_RETRY_DELAY = 60


@celery_client.task(bind=True, name="celery.collect_database")
def collect_database(self: Task, task_id: int, database_id: int) -> dict[str, Any]:
    """后台任务：把一个业务库整库采集进元数据库与向量库（登记表结构、分析字段、向量化、采集取值）。

    :param self: 本任务实例（``bind=True``）：已重试次数（``self.request.retries``）与重试入口
        （``self.retry``）都从它取
    :param task_id: 任务编号（雪花 ID）：任务记录（:class:`plugins.redis.Task`）按它归属，进度就写在
        这条记录上，调用方拿它轮询（见 controllers.evaluation.list_tasks）；记录本身由 API 在投递之前
        写好（类型、入参写在 meta 里），worker 沿用同一份入参、只往上追加进度帧与产出
    :param database_id: 要采集的业务库编号（Database.id）：整库采集，不挑表（见
        :meth:`plugins.agents.modeling.graph.Modeler.invoke` 的 table_ids）
    :return: ``{"steps": [...], "dataset_id": ..., "outputs": {...}, "error": ...}``：本轮的全部进度帧、
        产出（库编号与采集标记，见 :func:`run_collection`）与失败原因

    采集成功才把 Database.collected 置位（登记就此锁死，见 :func:`run_collection`）；采集失败时库还能改，
    改完再采一次即可。意外失败（连不上库之类的）重试 :data:`TASK_RETRIES` 次，仍不成才把记录落成 error
    （见 :func:`run_with_retry`）。
    """
    return run_with_retry(self, task_id, run_collection(task_id, database_id))


async def run_collection(task_id: int, database_id: int) -> dict[str, Any]:
    """在 worker 的常驻事件循环里整库采集一轮，逐帧把进度写进任务记录，返回本轮汇总。

    采集前先补登记该库的连接：worker 是独立进程、只在启动时登记过一批业务库（见
    :func:`plugins.celery.bootstrap_worker`），启动之后新登记的库它还不知道；连接是按别名惰性创建的，
    补登记一次即可（见 :func:`models.register_connection`）。

    产出是采集标记：整轮跑完（没有失败帧）就把 ``Database.collected`` 置位——登记就此锁死、不允许再改，
    这是「采集成功才算数」的口径：采集失败时库还能改，改完再采一次。库在任务排队期间被删掉时直接把
    任务记成失败（不必重试，重跑也是一样），不跑采集图。
    """
    inputs = {"database_id": database_id}
    database = await Database.get_or_none(id=database_id)
    if database is None:
        error = f"业务库 {database_id} 已不存在，无需采集"
        await record(task_id, TaskType.COLLECT_DATABASE, inputs, [], {},
                     status=TaskStatus.ERROR, error=error)
        return {"steps": [], "dataset_id": None, "outputs": {}, "error": error}
    await register_connection(database.id, database.url)
    summary = await run_graph(task_id, TaskType.COLLECT_DATABASE, inputs,
                              modeler.invoke(database_id=database_id))
    if summary["error"] is None:
        await Database.filter(id=database_id).update(collected=True)
        summary["outputs"].update({"database_id": database_id, "collected": True})
    await finish(task_id, TaskType.COLLECT_DATABASE, inputs, summary)
    return summary


@celery_client.task(bind=True, name="celery.create_dataset")
def create_dataset(self: Task, task_id: int, thread_id: int, table_ids: list[str], size: int,
                   database_id: int) -> dict[str, Any]:
    """后台任务：按指定的表跑一轮出题（生成问题与 SQL → 校验纠正 → 落库数据集），逐帧上报进度。

    :param self: 本任务实例（``bind=True``）：已重试次数（``self.request.retries``）与重试入口
        （``self.retry``）都从它取
    :param task_id: 任务编号（雪花 ID）：任务记录（:class:`plugins.redis.Task`）按它归属，进度就写在
        这条记录上，调用方拿它轮询（见 controllers.evaluation.list_tasks）；记录本身由 API 在投递
        之前写好（类型、入参写在 meta 里），worker 沿用同一份入参、只往上追加进度帧与产出
    :param thread_id: 会话编号（雪花 ID，由 API 生成，见 controllers.evaluation.create_dataset）：评估图
        按它把运行状态存进 checkpointer、落库的数据集也按它归属——之后就是拿它恢复评测
    :param table_ids: 出题所用的表编号（Table.id，形如 <库编号>.<表名>）：API 侧已校验存在并按编号排好序
    :param size: 生成的问题数量
    :param database_id: 本次出题所用业务库编号（Database.id），随图上下文带进去
    :return: ``{"steps": [...], "dataset_id": ..., "outputs": {...}, "error": ...}``：本轮的全部进度帧
        （Step 的字典形态）、落库的数据集编号、产出与失败原因

    出题失败不抛异常：原因在返回值的 error 里（进度帧本身也带），任务记录随即落 error 状态——
    调用方只看记录，不必去看 Celery 的任务状态。进度每帧一次、整体覆盖写进记录（见
    :func:`generate_dataset`），因此轮询随时能看到跑到哪了。意外失败（连不上库之类的）重试
    :data:`TASK_RETRIES` 次，仍不成才把记录落成 error（见 :func:`run_with_retry`）。
    """
    return run_with_retry(self, task_id, generate_dataset(task_id, thread_id, table_ids, size, database_id))


@celery_client.task(bind=True, name="celery.evaluate_dataset")
def evaluate_dataset(self: Task, task_id: int, dataset_id: int, thread_id: int, database_id: int) -> dict[str, Any]:
    """后台任务：按数据集的会话编号恢复那一轮运行，跑一轮评测并落一条结果，逐帧上报进度。

    :param self: 本任务实例（``bind=True``）：与出题任务同理，重试从它取
    :param task_id: 任务编号（雪花 ID）：与出题任务同一条约定，进度写在 ``Task`` 记录里（类型为
        :attr:`plugins.redis.TaskType.EVALUATE_DATASET`）
    :param dataset_id: 被评测的数据集编号（Dataset.id）：结果挂在它下面（Dataset.results），
        评完取该数据集最新的一条结果作为本轮的产出（见 :func:`run_evaluation`）
    :param thread_id: 会话编号（Dataset.thread_id，见 controllers.evaluation.evaluate_dataset）：评估图
        按它恢复出题那一轮的状态，出题所用的表、问题与参考 SQL 都在里面
    :param database_id: 本次评测的业务库编号（Database.id）：检索智能体在该库上答题
    :return: ``{"steps": [...], "dataset_id": ..., "outputs": {...}, "error": ...}``：本轮的全部进度帧、
        被评测的数据集编号、产出（结果编号与平均分）与失败原因

    同一个数据集可以反复评测，每投递一次就是一个独立的任务（各跑一轮、各落一条结果）。
    失败与重试的处置与出题任务完全一致，都交给 :func:`run_with_retry`。
    """
    return run_with_retry(self, task_id, run_evaluation(task_id, dataset_id, thread_id, database_id))


def run_with_retry(task: Task, task_id: int, coroutine: Coroutine[Any, Any, dict[str, Any]]) -> dict[str, Any]:
    """在 worker 的常驻事件循环里跑任务的协程，意外失败就交给 Celery 重试（两个后台任务共用）。

    :param task: 当前的 Celery 任务实例（``bind=True``）：重试次数取自 ``task.request.retries``，
        重试入口是 ``task.retry``
    :param task_id: 任务编号：重试用尽时把任务记录落成 error（见 :meth:`plugins.redis.Task.fail`）
    :param coroutine: 真正干活的协程（列出进度帧、写任务记录、返回本轮汇总）
    :return: 协程的返回值：``{"steps": [...], "dataset_id": ..., "error": ...}``

    重试期间任务记录保持 running（协程只在跑完时才写收尾那一笔，见 :func:`run_graph`）：重跑会从第一帧
    起重新覆盖进度，调用方看到的是一次「又动起来了」，而不是一次假的失败。重试用尽仍失败时，原因落进
    任务记录再抛出，让 Celery 也记一笔 FAILURE 便于排查。
    """
    try:
        return run(coroutine)
    except Exception as exception:
        if task.request.retries >= TASK_RETRIES:
            logger.exception(f"celery: 任务 {task_id} 重试 {TASK_RETRIES} 次仍失败")
            run(TaskRecord.fail(task_id, exception))
            raise
        logger.warning(f"celery: 任务 {task_id} 第 {task.request.retries + 1} 次重试，"
                       f"{TASK_RETRY_DELAY} 秒后重跑：{exception}")
        raise task.retry(exc=exception, countdown=TASK_RETRY_DELAY)


async def generate_dataset(task_id: int, thread_id: int, table_ids: list[str], size: int,
                           database_id: int) -> dict[str, Any]:
    """在 worker 的常驻事件循环里跑一轮出题，逐帧把进度写进任务记录，返回本轮汇总。

    评估图在 worker 启动时就初始化好了（见 :func:`plugins.celery.bootstrap_worker`），这里直接用模块级的
    :data:`evaluator`：开池、建表、编译图都不便宜，不能每个任务都来一遍。

    产出就是落库的数据集编号：进度帧一带上它（「数据集已保存」那帧），就写进 ``meta.outputs``，
    调用方不必等到任务收尾（见 :func:`run_graph`）。
    """
    inputs = {"thread_id": thread_id, "table_ids": table_ids, "size": size, "database_id": database_id}
    summary = await run_graph(task_id, TaskType.CREATE_DATASET, inputs,
                              evaluator.invoke(size, table_ids, database_id, thread_id))
    await finish(task_id, TaskType.CREATE_DATASET, inputs, summary)
    return summary


async def run_evaluation(task_id: int, dataset_id: int, thread_id: int, database_id: int) -> dict[str, Any]:
    """在 worker 的常驻事件循环里跑一轮评测，逐帧把进度写进任务记录，返回本轮汇总。

    恢复会话那条路（``evaluator.invoke`` 的 size 与 table_ids 都传 None）：出题所用的表、问题与
    参考 SQL 都在会话状态里，本次只跑评测节点。

    产出是这一轮评测的结果：逐条判定与平均分由评测节点落库（见
    ``plugins.agents.evaluation.nodes.evaluate_dataset``），这里取该数据集最新的一条结果作为本轮产出
    （同一个数据集反复评测各落一条、按编号递增，跑完的这条就是最新的）。评测没跑到落库（失败）时
    没有产出，只留失败原因。
    """
    inputs = {"dataset_id": dataset_id, "thread_id": thread_id, "database_id": database_id}
    summary = await run_graph(task_id, TaskType.EVALUATE_DATASET, inputs,
                              evaluator.invoke(None, None, database_id, thread_id))
    if summary["error"] is None and summary["dataset_id"]:
        # 产出要在收尾那一笔之前补齐：结果编号与平均分就是这一轮的产出（见 finish）
        result = await Result.filter(dataset_id=summary["dataset_id"]).order_by("-id").first()
        if result is not None:
            summary["outputs"].update({"result_id": result.id, "mean": result.mean})
    await finish(task_id, TaskType.EVALUATE_DATASET, inputs, summary)
    return summary


async def run_graph(task_id: int, task_type: TaskType, inputs: dict[str, Any],
                    stream: AsyncGenerator[Step, Any]) -> dict[str, Any]:
    """消费图的进度流：逐帧把进度写进任务记录，返回本轮汇总（三个后台任务共用）。

    :param task_id: 任务编号（见 :class:`plugins.redis.Task`）
    :param task_type: 任务类型（见 :class:`plugins.redis.TaskType`）：写进 ``meta.type``
    :param inputs: 投递时的入参，写进 ``meta.inputs``
    :param stream: 图的进度流（Evaluator / Modeler 各自 invoke 的产出，帧见各自模块的 schemas）
    :return: ``{"steps": [...], "dataset_id": ..., "outputs": {...}, "error": ...}``

    每收到一帧就整体覆盖写一次记录（类型 + 入参 + 到目前为止的全部帧 + 已经拿到的产出）：帧只有十几条、
    体积很小，整体写换来的是调用方一次轮询就能拿到完整过程，不必自己拼接。收尾那一笔（状态与补齐的产出）
    由调用方来写（见 :func:`finish`）：评测的产出要等结果落库后才能取到，只有调用方知道还要补什么。

    帧里的 dataset_id 只有出题 / 评测会带（采集图的帧没有这一栏，见
    plugins.agents.modeling.schemas.Step），故按「有没有这一栏」取：一旦出现（数据集落库那帧）就写进
    产出，调用方随时能对上数据集。
    """
    if evaluator.graph is None:
        raise RuntimeError("评估图尚未初始化：worker 启动时的初始化没成功，见 celery 日志里的注册失败原因")
    steps = []
    dataset_id, error = None, None
    outputs = {}
    async for step in stream:
        frame = asdict(step)
        steps.append(frame)
        if frame.get("dataset_id"):
            # 数据集编号一出（出题落库那帧）就写进产出，调用方随时能对上数据集
            dataset_id = frame["dataset_id"]
            outputs["dataset_id"] = dataset_id
        if frame["status"] == StepStatus.FAILED:
            error = frame["error"]
        await record(task_id, task_type, inputs, steps, outputs)
    logger.info(f"celery: 任务 {task_id}（{task_type}）跑完，产出 {outputs or None}，失败原因 {error}")
    return {"steps": steps, "dataset_id": dataset_id, "outputs": outputs, "error": error}


async def finish(task_id: int, task_type: TaskType, inputs: dict[str, Any],
                 summary: dict[str, Any]) -> None:
    """收尾那一笔：按本轮成败把任务记录定成 success / error，并把补齐后的产出写进去（见 :func:`record`）。

    业务失败（出题失败、评测失败）也是「跑完了」，故这里落 error 状态而不是留着 running：调用方看
    ``status`` 就知道要不要接着轮询。真正的意外失败不写在这里——重试期间记录停在 running，
    重试用尽才由 :func:`run_with_retry` 落 error。
    """
    error = summary["error"]
    await record(task_id, task_type, inputs, summary["steps"], summary["outputs"],
                 status=TaskStatus.ERROR if error else TaskStatus.SUCCESS, error=error)


async def record(task_id: int, task_type: TaskType, inputs: dict[str, Any], steps: list[dict[str, Any]],
                 outputs: dict[str, Any], status: TaskStatus = TaskStatus.RUNNING,
                 error: str | None = None) -> None:
    """写一条任务记录（整体覆盖）：类型、入参、进度帧与产出（见 :meth:`plugins.redis.Task.save`）。"""
    await TaskRecord(id=task_id, status=status, error=error, meta={
        "type": task_type, "inputs": inputs, "steps": steps, "outputs": outputs
    }).save()


@celery_client.task(name="celery.ping")
def ping() -> str:
    """占位任务：回一个 pong，用来确认 worker 已就绪、任务能被正常投递与执行。

    换成真正的异步任务时按同样的写法注册即可（``@celery_client.task(name=...)``）。
    """
    return "pong"


@celery_client.task(name="celery.scan_tables")
def scan_tables() -> dict[str, Any]:
    """定时巡检：比对业务库里的真实表结构与元数据库里的登记，把差异按表处置掉。

    比对维度是表名称、字段名称、字段类型与字段角色（普通字段 ↔ 主键 / 外键，见
    :func:`find_changed_tables`）；差异按表汇总，处置也按表来（见 :func:`reconcile_database`）：
    业务库里已移除的表直接清掉其名下字段的嵌入向量与元数据库里的登记，不必动用建模智能体
    （见 :func:`purge_tables`）；新增与变动的表交给建模智能体重采（见 :func:`remodel_tables`，
    用的就是整库采集那套图，只是把范围缩到这几张表）。

    一轮同时只跑一个（见 :func:`scan_exclusively`）。

    由 beat 投递（周期取自 ``config.CELERY_SCAN_INTERVAL``，调度表见
    :data:`plugins.celery.celery_client`）。

    :return: ``{业务库编号: {"added": [...], "removed": [...], "changed": [...]}}``：本轮查出差异的
        库（差异已处置；处置失败的库与尚未采集成功的库只记日志，见 :func:`scan_databases`）
    """
    return run(scan_exclusively())


async def scan_exclusively() -> dict[str, Any]:
    """抢到互斥锁才巡检，抢不到（上一轮还没结束）就跳过本轮，返回空报告。

    锁是 Redis 上的（见 :func:`plugins.redis.lock`），跨进程有效：prefork 的多个 worker 子进程
    抢的是同一把锁。上一轮巡检还没结束时抢不到锁，本轮直接跳过——巡检会重写元数据库与向量库
    （先删后建，见 :func:`reconcile_database`），少看一次只是晚一轮发现差异，下一轮照常；
    TTL 见 :data:`SCAN_LOCK_TTL`，兜底持有者被强杀时锁不会一直挂着。
    """
    async with lock(SCAN_LOCK_KEY, SCAN_LOCK_TTL, db=CELERY_BROKER_DB) as acquired:
        if not acquired:
            logger.warning("scan_tables: 上一轮巡检还没结束，跳过本轮")
            return {}
        return await scan_databases()


async def scan_databases() -> dict[str, Any]:
    """逐个业务库巡检并处置，返回 ``{业务库编号: {"added": [...], "removed": [...], "changed": [...]}}``。

    只收录真正有差异的库，键即元数据库里的 Database.id（表名的口径见 :func:`scan_database`）；
    查出来的差异当场交给 :func:`reconcile_database` 处置（差异怎么处置见 :func:`scan_tables`），
    故返回的是「本轮发现过的差异」，不是「还留着的差异」。

    某个库读不动（连接没登记、库被移走）只记一条日志跳过，不影响其它库；处置失败（向量库不可用、
    模型报错）同样只记日志，下一轮巡检会重新发现这批差异并再处置一次。

    还没采集成功的库（``Database.collected`` 为假）只报差异、不处置：这类库的登记本身还可能被改
    （见 controllers.modeling.update_database），它名下的表也会被整批当成新增——采不采由用户按需发起
    （``POST /modeling/database/{id}/collect``），巡检不该替它把整库的模型调用跑掉。
    """
    report = {}
    for database in await Database.all():
        try:
            differences = await scan_database(database)
        except Exception as error:  # noqa: BLE001 - 单个库读不动不该拖垮整轮巡检
            logger.warning(f"scan_tables: 巡检业务库 {database.name}（{database.id}）失败：{error}")
            continue
        if not any(differences.values()):
            continue
        report[database.id] = differences
        logger.warning(f"scan_tables: 业务库 {database.name}（{database.id}）与元数据库登记不一致："
                       f"新增 {differences['added']}、已删除 {differences['removed']}、"
                       f"变动 {differences['changed']}")
        if not database.collected:
            # 还没整库采集过：登记还能改、也没有可维护的采集产物，交给用户按需发起的整库采集
            logger.warning(f"scan_tables: 业务库 {database.name}（{database.id}）尚未采集成功，跳过处置")
            continue
        try:
            await reconcile_database(database, differences)
        except Exception as error:  # noqa: BLE001 - 单个库处置失败不该拖垮整轮巡检，下一轮会再发现
            logger.exception(f"scan_tables: 处置业务库 {database.name}（{database.id}）的差异失败：{error}")
    logger.info(f"scan_tables: 巡检完成，{len(report)} 个库与元数据库登记不一致")
    return report


async def scan_database(database: Database) -> dict[str, list[str]]:
    """比对单个业务库与元数据库里登记的表 / 字段。

    :param database: 元数据库里登记的业务库（models.meta.Database）
    :return: ``{"added": [表名], "removed": [表名], "changed": [表名]}``

    真实结构取自业务库自己的 information_schema（见 models.get_table_names / get_table_infos），
    连的是该库编号对应的连接（见 models.get_connection）。表名在库内唯一，
    业务库多出来的即新增、元数据库多出来的即已删除；两边都有的再逐字段比对（见
    :func:`find_changed_tables`），对不上即变动。表在业务库里改了名会表现为一张新增 + 一张已删除。

    只读（读业务库结构、读元数据库登记），报出差异而不处置：怎么处置交给调用方（见
    :func:`reconcile_database`）。
    """
    available = await get_table_names(database.id)
    registered = {table.name: table for table in
                  await Table.filter(database_id=database.id).only("id", "name")}
    names = set(available)
    return {
        "added": [name for name in available if name not in registered],
        "removed": [name for name in registered if name not in names],
        "changed": await find_changed_tables([name for name in available if name in registered],
                                             registered, database.id)
    }


async def find_changed_tables(table_names: list[str], registered: dict[str, Table],
                              database_id: int) -> list[str]:
    """挑出与元数据库登记对不上的表（字段名称、类型、角色任一不符，保持传入顺序）。

    :param table_names: 要核对的表名（两侧都已登记，即本库共有的表）
    :param registered: 表名 → 元数据库里的 Table（只用于取表编号，据此查已登记的字段）
    :param database_id: 业务库编号（Database.id）：读真实结构用（见 models.get_connection）
    :return: 对不上的表名

    两侧都归成「(字段名, 类型, 是否主键 / 外键) 的集合」再比，故一次覆盖三类差异：字段改名
    （旧名对不上、新名没登记）、字段类型变了、字段角色变了——业务库里从普通字段变成主键 / 外键，
    或元数据库里登记成主键 / 外键、业务库里其实不是。业务库一侧的键取自 information_schema
    （get_table_infos 返回的 keys），元数据库一侧取自 Column.role；两侧的类型同源
    （Column.type 存的就是 column_type），可以直接比对。字段顺序与重复不影响判定。
    """
    if not table_names:
        return []
    infos = await get_table_infos(table_names, database_id)
    actual = {info["name"]: {(column["name"], column["type"], is_business_key(column))
                             for column in info["columns"]}
              for info in infos}
    ids = {registered[name].id: name for name in table_names}
    expected = {name: set() for name in table_names}
    for column in await Column.filter(table_id__in=list(ids)).only("table_id", "name", "type", "role"):
        name = ids.get(column.table_id)
        if name is not None:
            expected[name].add((column.name, column.type, is_key_role(column.role)))
    return [name for name in table_names if actual.get(name) != expected[name]]


async def reconcile_database(database: Database, differences: dict[str, list[str]]) -> None:
    """按巡检结果处置一个业务库的差异（见 :func:`scan_database`）。

    :param database: 元数据库里登记的业务库（models.meta.Database）
    :param differences: 该库的差异，``{"added": [表名], "removed": [表名], "changed": [表名]}``

    - 已移除的表：清掉其名下字段的嵌入向量与元数据库里的登记（见 :func:`purge_tables`）。表都没了，
      留下的登记会让前端仍能选中它、检索也仍以为这些字段可用，只有清掉才与业务库对齐；
      这件事不需要模型，故也不走建模智能体；
    - 新增 / 变动的表：交给建模智能体重采（见 :func:`remodel_tables`）。新增的表要模型分析出角色、
      描述、别名与同步标记，变动的表要按业务库里的最新结构重写登记与向量——两者都只有模型能做。

    表编号按命名取：已登记的表沿用元数据库里的编号（变动的那批与已移除的那批），未登记的（新增的
    那批）按 ``<业务库编号>.<表名>`` 拼——与采集时给首次采集的表生成编号的口径一致（见
    plugins.agents.modeling.nodes.discover_tables）。表在业务库里改了名会表现为一张新增 + 一张已删除，
    两侧各按自己的口径处置，互不干扰。
    """
    registered = {table.name: table.id for table in
                  await Table.filter(database_id=database.id).only("id", "name")}
    if differences["removed"]:
        await purge_tables(database.id, [registered[name] for name in differences["removed"]])
    table_ids = [registered.get(name, f"{database.id}.{name}")
                 for name in [*differences["added"], *differences["changed"]]]
    if table_ids:
        await remodel_tables(database.id, table_ids)


async def purge_tables(database_id: int, table_ids: list[str]) -> None:
    """清掉已从业务库里移除的表：先删其名下字段的嵌入向量，再删元数据库里的登记。

    :param database_id: 这些表所属的业务库编号（Database.id）：向量按库分开存，字段点与取值点都从该库
        的集合里删（见 Qdrant.delete_columns / delete_values）
    :param table_ids: 已移除的表编号（Table.id，形如 <库编号>.<表名>）

    向量先删、登记后删：向量库清不动（Qdrant 不可用）时登记还在，下一轮巡检照样能查出这批表并再清一次，
    删除本身是幂等的；反过来先删登记就会留下再也清不掉、却仍会被 find_columns 召回的字段点。
    登记里字段与表分两步删（外键为 RESTRICT，得先删字段再删表，同删库的口径，见
    controllers.modeling.delete_database），两步放在一个事务里。登记一并删掉，表名与表编号才不再出现在
    表列表里；业务库里再建出同名的表时，下一轮巡检会把它当成新增表交给建模智能体重采。
    """
    column_ids = [column.id for column in await Column.filter(table_id__in=table_ids).only("id")]
    await qdrant.delete_columns(database_id, column_ids)
    await qdrant.delete_values(database_id, column_ids)
    async with in_transaction(META_CONNECTION):
        await Column.filter(table_id__in=table_ids).delete()
        await Table.filter(id__in=table_ids).delete()
    logger.info(f"scan_tables: purged {len(table_ids)} removed table(s) "
                f"({len(column_ids)} column(s)), database {database_id}")


async def remodel_tables(database_id: int, table_ids: list[str]) -> None:
    """把新增 / 变动的表交给建模智能体重采：重读表结构、覆盖元数据库登记、重写字段向量与取值。

    :param database_id: 这些表所属的业务库编号（Database.id）：采集节点按它读业务库结构、拼表编号
    :param table_ids: 待重采的表编号（Table.id）：新增的表按 ``<库编号>.<表名>`` 首次采集，变动的表沿用
        已登记的编号（先删后建覆盖旧登记，见 plugins.agents.modeling.nodes.discover_tables）

    用的就是整库采集那套图（见 :class:`plugins.agents.modeling.graph.Modeler`），只是把 table_ids 缩到
    这几张表：模型只分析它们，元数据库按「先删后建」覆盖旧登记，向量按字段编号先清旧点再写入，重复采
    同一张表也不会堆积。进度帧只记日志——巡检由 beat 投递、没有任务记录可写（见 :func:`scan_tables`）；
    采失败的表不会落库，下一轮巡检会重新发现它并重来一遍。
    """
    async for step in modeler.invoke(table_ids=table_ids, database_id=database_id):
        if step.status == ModelingStepStatus.FAILED:
            logger.warning(f"scan_tables: 重采表失败：{step.message}（{step.error}）")
    logger.info(f"scan_tables: remodeled {len(table_ids)} table(s) of database {database_id}")


def is_business_key(column: dict[str, Any]) -> bool:
    """业务库结构里该字段是不是主键 / 外键（键由 models.get_table_infos 展平，见其 keys）。"""
    return any(key.get("type") in ("PK", "FK") for key in column["keys"])


def is_key_role(role: ColumnRoleOption | None) -> bool:
    """元数据库里登记的字段角色是不是主键 / 外键（其余角色只由模型给出，不参与巡检比对）。"""
    return role in (ColumnRoleOption.PRIMARY_KEY, ColumnRoleOption.FOREIGN_KEY)
