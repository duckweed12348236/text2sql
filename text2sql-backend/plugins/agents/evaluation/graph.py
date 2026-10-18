from typing import Any, AsyncGenerator

# 导入即给 ragas 打依赖补丁，必须早于 ragas 的任何导入（见 ragas_compat 的说明）
from plugins.agents.evaluation import compat  # noqa: F401  # isort: skip
from loguru import logger
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import StateGraph
from langchain_openai.chat_models.base import BaseChatOpenAI
from openai import AsyncOpenAI
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from ragas.llms.base import llm_factory
from ragas.metrics.collections import SQLSemanticEquivalence

from config import POSTGRESQL_URL
from plugins import Singleton, Plugin
from plugins.agents.checkpointers import CachedAsyncPostgresSaver
from plugins.agents.evaluation.nodes import generate_questions, correct_sql, evaluate_dataset
from plugins.agents.evaluation.schemas import Context, State, Step
from plugins.agents.models import model
from plugins.agents.retrieval.graph import Retriever
from plugins.snowflake import Snowflake


def build_sql_equivalence(model: BaseChatOpenAI) -> SQLSemanticEquivalence:
    """按给定模型的连接信息装配 SQL 语义等效性指标，供评估图在调用时注入评测节点。

    :param model: 出题与生成 SQL 用的模型（agent 的默认模型）
    :return: ragas 的 SQL 语义等效性指标

    指标判定「两条 SQL 在同一数据库上执行是否得到相同结果」，分数 1.0 表示等效、0.0 表示不等效，
    判定理由由 ragas 写在 MetricResult.reason 里。

    ragas 的 collections 指标要求传入 instructor 包装的结构化输出客户端
    （InstructorBaseRagasLLM），与 agent 用的 langchain ChatOpenAI 不是同一套封装，无法直接复用，
    故这里从传入模型的连接信息（端点、密钥、模型名）另建一个 OpenAI 兼容客户端：
    指标与 agent 用的仍是同一个模型，默认模型换掉时指标随之一同切换。
    """
    client = AsyncOpenAI(
        api_key=model.openai_api_key.get_secret_value(),
        base_url=model.openai_api_base
    )
    return SQLSemanticEquivalence(llm=llm_factory(model.model_name, client=client))


class Evaluator(Plugin):
    """评估图的入口：按外部指定的表生成问题与 SQL，校验纠正后产出评测数据集，再用检索智能体评测。

    generate_questions → correct_sql → evaluate_dataset → 结束：三个节点返回的都是状态更新
    （Command(update=...) 或 Command(goto=END, update=...)），节点之间的推进由这里登记的静态边负责，
    出错时节点自行跳到 END 并把原因写进 state.error。
    出题用的表由调用方指定（invoke 的 table_ids 入参，见 controllers.evaluation.create_dataset）：
    图只把表编号带进状态，出题节点按编号组装所用的表（见
    plugins.agents.evaluation.nodes.build_table_infos）并把这份表信息写回状态，纠正直接沿用，
    评测开跑前再按编号刷新一遍。
    节点通过 runtime.stream_writer 上报进度帧（Step：message / status / error / dataset_id，见
    schemas.Step），这里逐条转发给调用方——失败也在帧里（status 为 failed、error 是原因），
    不另留报告、也不替节点处理失败。
    出题用的 LLM 取自 plugins.agents.models；待评测的检索智能体与按默认模型装配的评测指标
    在 invoke 时以 Context 注入，节点里从 runtime.context 取用。

    图带 checkpointer 且在评测前中断：跑到数据集落库为止就停住，带上同一个线程编号再调一次即为恢复
    ——延续这一轮执行评测，或再评测一轮（见 invoke）。
    会话状态交给短期记忆组件（见 plugins.agents.checkpointers）：PostgreSQL 是事实来源，
    Redis 做缓存副本，按会话编号清除两边的入口是 forget()（删除数据集接口走的就是它）——
    因此重启、多副本都不再丢会话。连接池、checkpointer 与图都在 init() 里装配（应用启动时调用），
    关池在 close() 里（应用关闭时调用）。
    """

    def __init__(self):
        # 图在 init() 里才编译：checkpointer 必须在运行中的事件循环里构造（AsyncPostgresSaver
        # 构造时会抓当前循环），而本类是导入时就实例化的——那一刻还没有循环
        self.graph = None
        self.pool = None
        self.saver = None

    async def register(self) -> None:
        """装配会话状态存储并编译图（应用启动时调用一次）：开池 → 建表 → 编译。

                池子按 langgraph 的硬性要求建：autocommit（建表里有 ``CREATE INDEX CONCURRENTLY``，
                不能跑在事务块里）+ dict_row（查询按列名取值）。评测前中断：interrupt_before 里的节点在
                开跑前暂停，状态存进 checkpointer，带上同一个线程编号再调一次即为恢复（见 invoke）。
                状态里带的是本包的自定义 dataclass，checkpoint 按 msgpack 序列化：显式允许这几个类型，
                否则恢复时会直接拒绝反序列化（见 JsonPlusSerializer 的说明）。
                """
        self.pool = AsyncConnectionPool(
            POSTGRESQL_URL,
            min_size=1,
            max_size=5,
            open=False,
            kwargs={"autocommit": True, "row_factory": dict_row}
        )
        await self.pool.open()
        self.saver = CachedAsyncPostgresSaver(self.pool, serde=JsonPlusSerializer(allowed_msgpack_modules=[
            ("plugins.agents.evaluation.schemas", "ColumnInfo"),
            ("plugins.agents.evaluation.schemas", "DatasetItemInfo"),
            ("plugins.agents.evaluation.schemas", "State"),
            ("plugins.agents.evaluation.schemas", "TableInfo"),
        ]))
        await self.saver.setup()

        graph_builder = StateGraph(State, context_schema=Context)
        graph_builder.add_node(generate_questions)
        graph_builder.add_node(correct_sql)
        graph_builder.add_node(evaluate_dataset)
        graph_builder.set_entry_point("generate_questions")
        graph_builder.add_edge("generate_questions", "correct_sql")
        graph_builder.add_edge("correct_sql", "evaluate_dataset")
        graph_builder.set_finish_point("evaluate_dataset")
        self.graph = graph_builder.compile(checkpointer=self.saver,
                                           interrupt_before=["evaluate_dataset"])
        logger.info("evaluator: 会话状态存储就绪（PostgreSQL + Redis 缓存）")

    async def close(self) -> None:
        """关闭会话状态存储占用的连接（应用关闭时调用）：先收 Redis 客户端，再收连接池。"""
        await self.saver.aclose()
        await self.pool.close()

    async def forget(self, thread_id: int) -> None:
        """清除某个会话的记忆：PostgreSQL 里的 checkpoint 与 Redis 里的缓存副本一起删掉。

        :param thread_id: 会话编号（雪花 ID，见 plugins.snowflake），与 invoke 用的是同一个：
            数据集删掉、或这一轮不再需要接着跑/接着评测时都可以调它（见
            controllers.evaluation.delete_dataset）。删完该会话即从记忆里消失，之后带同一编号
            调用 invoke 会当成全新的一轮。
        """
        await self.saver.adelete_thread(str(thread_id))

    async def invoke(self,
                     size: int | None,
                     table_ids: list[str] | None,
                     database_id: int,
                     thread_id: int | None = None) -> AsyncGenerator[Step, Any]:
        """流式产出评估过程：逐条转发节点上报的进度帧（Step，失败也在这串帧里）。

        :param size: 生成的问题数量；传 None 表示恢复停住的运行（继续评测，或再评测一轮，见下）
        :param table_ids: 出题所用的表编号（元数据库 Table.id）：调用方按编号取回表模型（见
            controllers.evaluation.create_dataset，那里同时做存在性校验），这里只把编号放进初始状态，
            表信息（字段与取值）由出题节点按编号组装（见
            plugins.agents.evaluation.nodes.build_table_infos）；恢复停住的运行时传 None 即可——
            要用的表编号已经在 checkpointer 的状态里
        :param database_id: 本次评测的业务库编号（Database.id）：待评测的检索智能体在该库上回答问题
            （随状态交给图，见 schemas.State.database_id）
        :param thread_id: 会话编号（雪花 ID，见 plugins.snowflake）：checkpoint 按它归属，同一个编号即同一轮
            运行的延续，新的编号即新一轮出题；出题落库的数据集也按它归属（见 models.dataset.Dataset.thread_id），
            节点要写它时直接从运行配置里取（见 nodes.correct_sql）

        一轮分成两步：第一次调用（size 为题数、table_ids 为所用表）出题 → 校验纠正 → 数据集落库 →
        **在评测前停住**，进度帧以「数据集已保存」（success，带 dataset_id）收尾；带上同一个 thread_id、
        size 传 None 再调一次即为恢复，执行一轮评测并把结果落库（一条 Result），进度帧以
        「评测结果已保存」收尾。同一线程可以反复恢复：上一轮评测已经跑完时先把状态标回「纠正完成」
        再跑一轮，因此同一个数据集可以换不同的检索智能体反复评测。

        每帧都是一条 Step（便于直接作为 SSE 数据帧），调用方按收尾那帧判断这一轮是停在评测前还是
        跑完了一轮评测；评测阶段的帧都带 dataset_id，出题阶段从数据集落库那帧起带上。
        """
        # langgraph 的 thread_id 收字符串，这里用会话编号的十进制文本（见 plugins.snowflake）
        if not thread_id:
            thread_id = Snowflake.get_id()
        config = {"configurable": {"thread_id": str(thread_id)}}
        state = await self.graph.aget_state(config)
        if size is None and state.values and not state.next:
            # 上一轮评测已经跑完：把状态标回 correct_sql 的产出，重新停在评测前，再跑一轮评测
            await self.graph.aupdate_state(config, None, as_node="correct_sql")
        if size is None and state.values.get("database_id") != database_id:
            # 库编号随状态走：把编号挪进状态之前建的会话里没有这个通道，恢复时用本次入参补写上，
            # 否则节点取不到它。以 correct_sql 的名义写（它是停住前最后跑过的节点），
            # 待跑的评测节点不会被这一步搅掉
            await self.graph.aupdate_state(config, {"database_id": database_id}, as_node="correct_sql")

        # 初始状态：出题那一轮把表编号与库编号放进来，表信息由出题节点组装并写回状态；
        # 恢复停住的运行（size 为 None）不带初始状态——要用的东西已经在 checkpointer 的状态里
        initial = None if size is None else State(size=size, table_ids=list(table_ids or []),
                                                 database_id=database_id)
        # 指标在调用时装配，用的是与出题相同的默认模型（见 metric.build_sql_equivalence）；
        # 库编号与会话编号都不在这里（前者随状态走、后者节点从运行配置里取）
        context = Context(
            model=model,
            retriever=Retriever(),
            metric=build_sql_equivalence(model)
        )
        logger.info(f"invoke: thread {thread_id}, "
                    f"{'新一轮出题' if size is not None else '恢复评测'}")
        async for chunk in self.graph.astream(initial, context=context, config=config, stream_mode="custom"):
            yield chunk
