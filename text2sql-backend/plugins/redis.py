"""Redis 基础设施：RedisOM 模型定义、索引迁移与跨进程互斥锁。

所有 Redis 操作都定义在本模块：模型（Schema）、迁移入口与互斥锁；每个模型的读写方法挂在模型
自己身上（见 :class:`ShortTermMemory` / :class:`Task`）。RedisOM 的连接挂在模型类的
``Meta.database`` 上：:class:`ShortTermMemory` 由使用方在初始化时注入（见
:class:`plugins.agents.checkpointers.CachedAsyncPostgresSaver`），:class:`Task` 由
:meth:`Task.setup` 自己建一份并常驻（API 与 worker 都要读写）；:func:`migrate` 与 :func:`lock`
这类一次性操作用到连接时按需创建、用完即关。

用法::

    from plugins.redis import ShortTermMemory, Task, lock

    ShortTermMemory.Meta.database = redis_client
    record = await ShortTermMemory.get("thread_id:checkpoint_ns:latest")
    await Task.setup()  # 任务记录的连接与索引，启动时调一次（见 plugins.celery）
    tasks = await Task.list()  # 按用户批量查任务
    async with lock("celery:scan_tables:lock", 3600) as acquired:
        ...  # 抢到锁才做事

Note:
    模型基于 RedisJSON（``JSON.SET`` / ``JSON.GET``），要求 Redis 已加载
    ReJSON 模块（Redis Stack / ``redis-stack-server`` 镜像）。裸 Redis 上
    缓存读写会失败，按事务方的 fail-open 策略降级为直连 PostgreSQL；
    :func:`migrate` 同样只告警、不阻断启动。任务记录的读写不吃 fail-open：
    没有 Redis 就没有进度可读，写不进去只能让调用方自己失败；按用户批量查
    任务还要 RediSearch 索引（见 :meth:`Task.setup`）。
"""

from contextlib import asynccontextmanager
from enum import StrEnum
from typing import Any, AsyncIterator

from aredis_om import Field, JsonModel, Migrator, NotFoundError
from redis.asyncio import Redis
from redis.asyncio.client import Pipeline
from redis.exceptions import LockNotOwnedError

import config
from logger import logger

__all__ = ["DEFAULT_USER_ID", "ShortTermMemory", "TASK_LIMIT", "TASK_TTL", "Task", "TaskStatus",
           "TaskType", "build_redis_url", "lock", "migrate"]

# 任务记录的保留时长（秒）：最多留 3 天，到期即从 Redis 里消失（见 Task）
TASK_TTL = 3 * 24 * 60 * 60
# 按用户批量查任务时一次取多少条（新的在前，见 Task.list）
TASK_LIMIT = 20
# 任务所属用户编号的默认值：用户体系还没有，当前一律当 1 号用户的任务（见 Task.user_id）
DEFAULT_USER_ID = 1


def build_redis_url(db: int | None = None) -> str:
    """按 :mod:`config` 中的 Redis 配置拼接连接串。

    同一个 Redis 实例按用途分库存放（0 号库放检索索引与缓存，Celery 的 broker 与结果后端
    各占一个库），地址、端口与口令统一来自 :mod:`config`，调用方只覆盖库号；``db`` 缺省
    用 :data:`config.REDIS_DB`。
    """
    auth = f":{config.REDIS_PASSWORD}@" if config.REDIS_PASSWORD else ""
    return f"redis://{auth}{config.REDIS_HOST}:{config.REDIS_PORT}/{config.REDIS_DB if db is None else db}"


@asynccontextmanager
async def lock(key: str, ttl: int, db: int | None = None) -> AsyncIterator[bool]:
    """跨进程互斥锁：进入时抢锁（yield 是否抢到），退出时释放（没抢到就不放）。

    :param key: 锁名。自带用途前缀（如 ``celery:scan_tables:lock``），别与检索索引 / 缓存的键撞名
    :param ttl: 兜底过期时间（秒）：持有者被强杀时没人会来释放锁，靠它自然失效，
        免得后来者永远抢不到
    :param db: 库号，缺省用 :data:`config.REDIS_DB`；换成别的库（如 broker 的库号
        ``config.CELERY_BROKER_DB``）即把锁放到那个库上
    :yield: 是否抢到锁。抢不到时调用方应跳过这次任务，而不是等待

    抢锁是 ``SET NX PX``（redis-py 的 ``Lock``）且不阻塞：抢不到立刻返回 False，等不等由调用方
    决定。释放只在退出时进行、且只放自己持有的那把锁——锁已因 ttl 过期被别人接管时，
    redis-py 会以 :class:`redis.exceptions.LockNotOwnedError` 拒绝，这里忽略即可
    （那把锁不归我们，放掉反而会拆掉别人的锁）。

    连接按需创建、用完即关，与 :func:`migrate` 一样不在模块里常驻，因此可安全用于
    API 进程与 worker 进程（含 prefork 的各个子进程）。
    """
    redis = Redis.from_url(build_redis_url(db), decode_responses=True)
    try:
        mutex = redis.lock(key, timeout=ttl)
        acquired = await mutex.acquire(blocking=False)
        try:
            yield acquired
        finally:
            if acquired:
                try:
                    await mutex.release()
                except LockNotOwnedError:
                    pass
    finally:
        await redis.aclose()


class ShortTermMemory(JsonModel, index=True):
    """短期记忆缓存记录：一条记录承载一个 checkpoint 的完整状态。

    记录以 JSON 文档存放，``payload`` 是 langgraph ``serde`` 序列化后的负载
    （类型名见 ``serde_type``），内容与 PostgreSQL 中持久化的 checkpoint
    一致，可无损还原 ``CheckpointTuple``；RedisOM 会把 ``bytes`` 字段按
    base64 存取，二进制负载不会撞上 JSON 的编码限制。``checkpoint_id``
    单独成字段，便于用一次 ``JSON.GET <key> $.checkpoint_id`` 判断“最新”
    记录是否仍指向它。

    键名由 ``Meta`` 前缀与主键拼成 ``stm:memory:{pk}``。``index=True`` 是
    RedisOM 对 ``save()`` / ``key()`` 的硬性要求；读写都走主键、不做
    ``find()`` 检索，索引只由 :func:`migrate` 按需建立。

    Note:
        RedisOM 的元数据挂在类上（``Meta.database``），因此同一进程内应
        共用同一个 Redis 连接；重新注入会同时影响所有使用方。
    """

    class Meta:
        global_key_prefix = "stm"
        model_key_prefix = "memory"
        database = None  # 由使用方注入

    checkpoint_id: str
    serde_type: str
    payload: bytes


class TaskStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    ERROR = "error"


class TaskType(StrEnum):
    """后台任务的类型（``Task.meta["type"]``）：取值与投递时用的 Celery 任务名一一对应（见 plugins.celery.tasks）。"""

    CREATE_DATASET = "create_dataset"
    EVALUATE_DATASET = "evaluate_dataset"
    COLLECT_DATABASE = "collect_database"


class Task(JsonModel, index=True):
    """后台任务的进度记录：类型、入参、进度帧与产出都在 ``meta`` 里，落 Redis，由轮询接口按用户批量查。

    ``id`` 是任务编号（雪花 ID，见 :mod:`plugins.snowflake`，由 API 生成），并且就做主键——RedisOM
    支持把业务字段声明成主键（``Field(primary_key=True)``），于是不必再多一个 ``pk`` 字段：
    ``Task.get(编号)`` 取记录、``Task.id == ...`` 查记录，一个编号走到底，Redis 里也是
    ``stm:task:{编号}`` 一条记录。

    字段与索引：

    - ``id``：主键，且 ``sortable``——按用户批量查任务时要按编号倒序取最新的几条（见 :meth:`list`）；
    - ``user_id``：任务归属的用户编号，``index`` 进 RediSearch，批量查询就按它筛（见 :meth:`list`）；
      用户体系还没做，当前一律取 :data:`DEFAULT_USER_ID`（1）；
    - ``status``：见 :class:`TaskStatus`——跑到最后落 success 或 error，重试期间仍是 running
      （见 :mod:`plugins.celery.tasks`）；``error`` 是失败原因，仅 error 状态有值；
    - ``meta``：任务的里子，都由写入方整体给出（见 :meth:`save`）——``type`` 是任务类型
      （见 :class:`TaskType`）；``inputs`` 是投递任务时的入参（出题是所用的表、题数、业务库与会话编号，
      评测是被评数据集、业务库与会话编号，见 ``controllers.evaluation``），任务还在队列里时轮询就能
      看到它是什么任务；``steps`` 是到目前为止的全部进度帧（``Step`` 的字典形态，见
      ``plugins.agents.evaluation.schemas``），执行方每收到一帧就整体覆盖写一次，读的一方因此一次
      就能拿到完整过程，不必自己拼接；``outputs`` 是本轮产出（出题是 ``dataset_id``，评测是
      ``result_id`` 与 ``mean``），跑出产出的那一刻起就在里面。

    记录留 :data:`TASK_TTL`（3 天，见 :meth:`save`），到期即消失，轮询方看到的只是「没有这个任务」；
    批量查询也要索引在（见 :meth:`setup`）。

    Note:
        RedisOM 的元数据挂在类上（``Meta.database``），连接由 :meth:`setup` 在启动时注入，
        同一进程内共用一份即可，不要各处重新注入。
    """

    class Meta:
        global_key_prefix = "stm"
        model_key_prefix = "task"
        database = None  # 由 setup 注入

    id: int = Field(primary_key=True, sortable=True, description="任务编号（雪花 ID）")
    user_id: int = Field(default=DEFAULT_USER_ID, index=True, description="任务所属用户编号（暂固定为 1）")
    error: str | None = Field(default=None, description="失败原因，仅 status 为 error 时有值")
    status: TaskStatus = Field(default=TaskStatus.RUNNING, description="任务状态")
    meta: dict = Field(description="任务元信息：type（类型）、inputs（入参）、steps（进度帧）、outputs（产出）")

    @classmethod
    async def setup(cls, db: int | None = None) -> None:
        """把任务记录挂到 Redis 上（应用启动与 worker 启动时各调一次，见 plugins.celery.bootstrap_worker）。

        :param db: 库号，缺省用 :data:`config.REDIS_DB`（与检索索引、缓存同库）

        连接自己建一份常驻：任务记录在三个地方都要读写——API 进程投递任务、API 进程轮询进度、worker
        进程上报进度，且与评估图的 checkpointer 没有关系（那份连接由评估图持有、生命周期跟着它），
        因此不挂在别人身上；重复调用不会重建（``Meta.database`` 已经有了就沿用）。

        顺手调一次 :func:`migrate` 建好 RediSearch 索引：:meth:`list` 是按 ``user_id`` 索引查的
        （不是扫全部键），索引没建起来时只能查到空。建索引是幂等的，失败只告警（见 :func:`migrate`）。
        """
        if cls.Meta.database is None:
            cls.Meta.database = Redis.from_url(build_redis_url(db), decode_responses=True)
        await migrate()

    async def save(self, pipeline: Pipeline | None = None, nx: bool = False, xx: bool = False) -> "Task | None":
        """写入任务记录（整体覆盖），顺手续期到 :data:`TASK_TTL`——重写 RedisOM 的 save，让「写一次留 3 天」成为唯一入口。

        整体覆盖是刻意的：写入方只有任务的执行者（或投递任务的 API）一个，每次带上「到目前为止的全部
        进度」，读的一方（轮询接口）因此总能拿到完整的一份，也不会读到写了一半的记录；续期放在这里，
        调用方不必记着还能活多久，跑得久、写得勤的任务自然就留得久。

        :param pipeline: 可选的 Redis 管道（与 RedisOM 的 save 一致，便于和别的写入批量提交）
        :param nx: 只在键不存在时写入（同 RedisOM）
        :param xx: 只在键已存在时写入（同 RedisOM）
        :return: 写入成功返回自己；``nx`` / ``xx`` 条件不满足时返回 ``None``（同 RedisOM，也顺带不续期）
        """
        saved = await super().save(pipeline=pipeline, nx=nx, xx=xx)
        if saved is None:
            return None
        await self.expire(TASK_TTL, pipeline=pipeline)
        return self

    @classmethod
    async def list(cls, user_id: int = DEFAULT_USER_ID, limit: int = TASK_LIMIT) -> "list[Task]":
        """批量查询某个用户的任务记录，新的在前（编号倒序），最多 :data:`TASK_LIMIT` 条。

        :param user_id: 用户编号，缺省取 :data:`DEFAULT_USER_ID`（用户体系还没有，当前都是 1 号用户）
        :param limit: 取多少条（新的优先）——任务记录小、轮询又要看得见，故按条数截断而不是全量返回

        走的是 ``user_id`` 上的 RediSearch 索引（见 :meth:`setup`），按 ``id`` 倒序排（要 sortable，
        见字段定义）；过期的记录不在结果里（键已经没了）。
        """
        return await cls.find(cls.user_id == user_id).sort_by("-id").page(0, limit)

    @classmethod
    async def fail(cls, task_id: int, error: str | BaseException) -> None:
        """把任务记为失败（重试用尽时调用，见 :func:`plugins.celery.tasks.create_dataset`）。

        记录里已经写下的入参与进度帧原样保留——跑到哪、错在哪都要留给轮询方看，故这里是读出来改状态、
        再整体写回，而不是新写一份。记录已经过期时（超过 :data:`TASK_TTL`）没什么可保留的，
        按空的一份写回去：该编号的任务确实失败了，轮询方查得到这个结论。
        """
        try:
            task = await cls.get(task_id)
        except NotFoundError:
            task = cls(id=task_id, meta={"inputs": {}, "steps": []})
        task.status = TaskStatus.ERROR
        task.error = str(error)
        await task.save()

    def dump(self) -> dict[str, Any]:
        """任务快照：编号、所属用户、状态、失败原因与元信息（入参 + 进度帧），即轮询接口的一条响应。"""
        return {"id": self.id, "user_id": self.user_id, "status": self.status, "error": self.error,
                "meta": self.meta}


async def migrate() -> None:
    """按模型定义建立/更新 RediSearch 索引（由 :meth:`Task.setup` 在启动时调用）。

    迁移是幂等的：RedisOM 把每个模型的索引 schema 哈希记在 Redis 中，只有
    索引缺失或 schema 变更时才重建（``DROP`` + ``CREATE``），因此可以放心地
    在每次启动时执行。

    Redis 未加载 RediSearch 模块时迁移会失败，这里捕获后只记录告警，不阻断
    启动：:class:`ShortTermMemory` 的读写走主键、用不上索引，:class:`Task`
    的按主键读写也一样——但 :meth:`Task.list`（按用户批量查任务）走的就是索引，
    索引没建起来时它只能查到空。迁移使用的连接按 :mod:`config` 临时创建，
    执行完即关闭，不影响后续注入到模型上的连接。

    Note:
        Redis 只允许在 0 号库创建检索索引：``config.REDIS_DB`` 非 0 时迁移
        必然失败（同样只告警），此时模型仍可正常读写。
    """
    db = Redis.from_url(build_redis_url(), decode_responses=True)
    try:
        await Migrator(conn=db).run()
        logger.info("Redis 模型迁移完成：索引已就绪")
    except Exception as exc:  # noqa: BLE001 - 迁移失败不应阻断应用启动
        logger.warning("Redis 模型迁移失败（仅影响索引检索，主键读写不受影响）: {}", exc)
    finally:
        await db.aclose()
