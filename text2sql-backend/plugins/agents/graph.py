from collections.abc import AsyncGenerator
from typing import Any

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import StateGraph
from langgraph.types import Command
from loguru import logger
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from config import POSTGRESQL_URL
from models.meta import ColumnRoleOption, TableRoleOption
from plugins import Plugin
from plugins.agents.callbacks import TimingHandler
from plugins.agents.models import bge_large_zh_embeddings, model, qwen_rerank
from plugins.agents.nodes import analyse_intention, rewrite_question, extract_keywords, recall_columns, \
    recall_metrics, rerank_tables, rerank_metrics, validate_metrics, ask_metric_definition, filter_tables, \
    recognise_relations, add_extra_info, generate_sql, validate_sql, correct_sql, execute_sql
from plugins.agents.schemas import ColumnInfo, Context, DatabaseInfo, DateInfo, MetricInfo, Mode, State, \
    Step, StepStatus, TableInfo
from plugins.snowflake import Snowflake

# checkpoint（msgpack）允许反序列化的自定义类型：状态里的 dataclass、它们字段上的枚举，以及枚举所在的模块。
# 不在这里列出来的类型，恢复会话时会被 JsonPlusSerializer 拒绝，日志里是那条
# 「Blocked deserialization of ... - not in allowed_msgpack_modules」——会话能存进去、却续不上。
# 新增进状态的类型（dataclass / 枚举）时，记得同时补到这里。
CHECKPOINT_ALLOWED_MODULES = [
    *[("plugins.agents.schemas", x.__name__)
      for x in (ColumnInfo, DatabaseInfo, DateInfo, MetricInfo, Mode, State, StepStatus, TableInfo)],
    # TableInfo.role 与 ColumnInfo.role 是这两个枚举（见 models.meta）
    ("models.meta", TableRoleOption.__name__),
    ("models.meta", ColumnRoleOption.__name__)
]


class Retriever(Plugin):
    """检索图的入口：把用户的自然语言问题转成 SQL 并执行。

    analyse_intention → rewrite_question → extract_keywords →┬→ recall_metrics → rerank_metrics ─────────────┐
                                                            └→ recall_columns → rerank_tables → filter_tables ┴→ validate_metrics →
    ask_metric_definition ↺（问口径 → 审核 → 不行就跳回自己再问）─┬→ add_extra_info → generate_sql →
                                                                  └→ recognise_relations → 结束
    generate_sql → validate_sql → execute_sql → 结束：
    节点之间的跳转写在节点内部（Command(goto=...)），与检索图自身的节点同一套写法；这里只负责注册节点、
    指定入口与终点，外加两路汇合的 join 边（见下）并编译。
    生成 SQL 之前还有一道「召回够不够」的检查（见 nodes.probe_recall）：把原始问题与当前候选表一起交给模型，
    判为不够用就让模型给出一批新的问题与关键词，跳回 recall_columns 再来一轮（上限 nodes.MAX_RECALL_RETRY）；
    补召回跑过之后，生成 SQL 用的是原始问题（State.original_question），而不是补召回临时改写的那一版，
    免得 SQL 跟着临时表述跑偏。
    意图识别确认问题适合查询后，先过一道问题改写（rewrite_question）：把「创新发展中心的员工有哪些」这类省略的说法
    补完整——推测「创新发展中心」是部门、补成「创新发展中心部门」，推测「员工有哪些」其实想看姓名与工号并写进问题；
    改写后的问题替换 state.question，后面的关键词提取、召回与 SQL 生成都按它来。改写失败或没必要改时原样往下走
    （见 nodes.rewrite_question）。
    两路并行：extract_keywords 一次派出指标与字段两路（Command(goto=[...])），指标一路是「召回 → 重排」，
    字段一路是「召回 → 重排 → 过滤」，互不依赖、谁也不用等谁；两路都在 validate_metrics 汇合，该节点由多起点的
    join 边（add_edge(["rerank_metrics", "filter_tables"], "validate_metrics")）触发，两侧都收口才核对口径。
    补召回时**只重跑字段那一路**（recall_columns → rerank_tables → filter_tables，见 nodes.probe_recall）：
    指标口径首轮已核对过，重跑只换「看什么」；这一遍 filter_tables 会因为 probe_count > 0 直接转到汇合之后的
    双路派发（add_extra_info + recognise_relations），不再等 join——指标那一路没重跑，join 永远等不到它。
    汇合之后又是一次双路派发：add_extra_info（补库与日期）与 recognise_relations（识别表间关系并落库）并行跑——
    前者接着生成 SQL，后者写完就 goto END：本轮生成 SQL 用的是召回时已登记的关系（见 nodes.recall_columns 的
    load_related_refs），新识别出来的留给下一次，不必挡在生成 SQL 前面。
    召回字段与取值在同一个节点里完成（recall_columns）：字段按关键词在 columns.<编号> 集合里召回；取值不再按
    关键词匹配，而是拿召回到的同步字段编号整批去 values.<编号> 里取回各字段的全部取值范围（一个字段一个点，
    见 Qdrant.add_values）——两者按表编号组装成候选表；指标走本库的 metrics.<编号> 集合（与字段、取值一样
    按库分开存，见 Qdrant.find_metrics）。
    指标与字段两路都各自重排：指标那一路重排（rerank_metrics）后在 validate_metrics 按问题口径过滤——先认出
    用户输入涉及哪些指标，再看召回的候选里有没有口径一致的：全对上就只留这几条，问题不需要指标就把召回的全
    清掉；有对不上的（含一条都没召回到）就把反问写进 state.clarification 交给 ask_metric_definition——那里
    interrupt 停住、拿到用户的回答后当场审核并落库，不采纳就换个问法跳回自己再问，重问超过 nodes.MAX_ASK_RETRY
    次才放弃（口径确认后 retry_count 清零，SQL 纠错从零开始数）；字段那一路则先在取值召回之后重排候选表
    （rerank_tables，交给重排模型的是组装好的表），再按「字段落点」细筛表（filter_tables），两路做完才进
    validate_metrics 汇合；汇合之后并行做两件事：补库与日期信息（add_extra_info）和识别表间关系
    （recognise_relations，把能 JOIN 的字段写进 ColumnRelation），后者写完即结束本轮。

    图编译两份、节点与边完全相同，差别只有 checkpointer：

    - ``graph``（带 checkpointer，HTTP 用）：指标口径反问要「停下来等用户、下一次请求接着跑」，而 langgraph 的
      interrupt 依赖 checkpoint 才能续跑，故这张图必须带；client 用 thread_id 把后续回答接回同一轮（见 invoke）。
    - ``eval_graph``（不带 checkpointer，评估工具用它批量答题）：评估是批量跑的，没有人来回答反问
      （见 nodes.validate_metrics），不需要续跑；也不该把每一轮评测的状态写进会话存储——并发评测同一批
      问题还会撞进同一个命名空间互相覆盖。

    会话状态存 PostgreSQL；这里不叠 Redis 缓存：检索会话只被恢复一两次，直连足够。
    连接池、saver 与两张图都在 register() 里装配（应用启动时调用），关池在 close() 里。

    LLM、嵌入模型与重排模型取自 plugins.agents.models，运行时以 Context 注入，节点里从 runtime.context 取用；
    检索哪个库、哪种模式属于这一轮的输入，随初始状态交给图（见 State.database_id / State.mode）。
    评估模式下 analyse_intention 不调用模型、直接跳到 extract_keywords，相当于跳过意图识别；validate_metrics
    与 ask_metric_definition 也跳过（没人来回答反问）；SQL 只做 EXPLAIN 校验、不调用大模型做合规校验，
    但会照常执行——既确认 SQL 真能跑通，也让「执行完成」那一帧带上 SQL 与检索到的表片段（见 eval）。
    校验失败时由节点自行回到 correct_sql 重新生成（重试上限见 nodes.MAX_RETRY）。
    """

    def __init__(self):
        # 图在 register() 里才编译：带 checkpointer 的图要求连接池在运行中的事件循环里建，而本类是导入时
        # 就实例化的——那一刻还没有循环
        self.graph = None
        self.eval_graph = None
        self.pool = None
        self.saver = None

    async def register(self) -> None:
        """装配会话状态存储并编译两张图（应用启动时调用一次）。

        连接池按 langgraph 的硬性要求建：autocommit + dict_row（建表语句里有 ``CREATE INDEX CONCURRENTLY``，
        不能跑在事务块里；查询按列名取值）。状态里有本包的自定义 dataclass，checkpoint 按 msgpack 序列化，
        必须显式允许这些类型，否则恢复时会被拒绝反序列化（见 JsonPlusSerializer 的说明）。
        """
        self.pool = AsyncConnectionPool(
            POSTGRESQL_URL,
            min_size=1,
            max_size=5,
            open=False,
            kwargs={"autocommit": True, "row_factory": dict_row}
        )
        await self.pool.open()
        self.saver = AsyncPostgresSaver(self.pool, serde=JsonPlusSerializer(
            allowed_msgpack_modules=CHECKPOINT_ALLOWED_MODULES))
        await self.saver.setup()
        self.graph = self.build().compile(checkpointer=self.saver)
        self.eval_graph = self.build().compile(checkpointer=False)
        logger.info("retriever: 会话状态存储就绪（checkpoint 落 PostgreSQL）")

    async def close(self) -> None:
        """关闭会话状态存储占用的连接池（应用关闭时调用）。"""
        if self.pool is not None:
            await self.pool.close()

    async def forget(self, thread_id: str) -> None:
        """清除某个会话的记忆：把这个 thread_id 下的全部 checkpoint 删掉。

        :param thread_id: 会话编号（与 invoke 用的是同一个，十进制文本）
        :raises RuntimeError: 会话状态存储尚未装配（应用启动时没走到 register）

        删完该编号即从记忆里消失：之后带同一个编号调用 invoke 会当成全新的一轮（从意图识别开跑），
        而不是接着上一次停住的地方恢复。删除数据集 / 会话登记时走它（见 controllers.retrieval.delete_session），
        免得留下的旧状态被下一次提问当成「恢复」接上去。

        只动会话状态，不动业务侧的会话登记（models.session）：两者按同一个 thread_id 对齐，谁删谁留由调用方决定。
        """
        if self.saver is None:
            raise RuntimeError("会话状态存储尚未装配：应用启动时应先调用 register()")
        await self.saver.adelete_thread(str(thread_id))
        logger.info(f"forget: thread {thread_id} 的记忆已清除")

    @staticmethod
    def build() -> StateGraph:
        """按链路装配图：跳转写在节点内部，这里只管注册节点、入口与终点，以及两路并行的 join 边。"""
        graph_builder = StateGraph(State, context_schema=Context)
        graph_builder.add_node(analyse_intention)
        # 意图确认后再把问题改写完整（补实体类型、补要看的属性），关键词提取按改写后的问题抽词
        graph_builder.add_node(rewrite_question)
        graph_builder.add_node(extract_keywords)
        # 两路并行：指标（召回 → 重排）与字段（召回 → 重排 → 过滤）各走各的，最后在 validate_metrics 汇合
        graph_builder.add_node(recall_metrics)
        graph_builder.add_node(rerank_metrics)
        graph_builder.add_node(recall_columns)
        graph_builder.add_node(rerank_tables)
        graph_builder.add_node(filter_tables)
        # 汇合点：两路都收口才核对指标口径（对不上就停下来问用户，见 nodes.validate_metrics）
        graph_builder.add_node(validate_metrics)
        graph_builder.add_edge(["rerank_metrics", "filter_tables"], "validate_metrics")
        # 问口径与审核合在一个节点里，答不到点子上会 Command(goto=自己) 再问一次（见 nodes.ask_metric_definition）
        graph_builder.add_node(ask_metric_definition)
        # 汇合之后双路派发：补库与日期（接着生成 SQL）和关系识别（写完即结束）并行跑（见类文档）
        graph_builder.add_node(add_extra_info)
        graph_builder.add_node(recognise_relations)
        graph_builder.add_node(generate_sql)
        graph_builder.add_node(validate_sql)
        graph_builder.add_node(correct_sql)
        graph_builder.add_node(execute_sql)
        graph_builder.set_entry_point("analyse_intention")
        # 终点：执行完 SQL 就结束（结果自查 / 数据探测节点已删；重召回改在生成 SQL 之前做，见 nodes.probe_recall）
        graph_builder.set_finish_point("execute_sql")
        return graph_builder

    async def invoke(self,
                     question: str,
                     database_id: int,
                     thread_id: str | None = None) -> AsyncGenerator[Step, Any]:
        """逐条产出检索进度（SSE 数据帧，与采集流程的输出格式一致）。

        :param question: 用户这一轮的输入；带 thread_id 恢复时，它就是用户对上一个反问（指标定义）的回答
        :param database_id: 本次检索的业务库编号（Database.id）：字段与取值都在该库的向量集合里召回
        :param thread_id: 会话编号（雪花 ID 的十进制文本，见 controllers.retrieval.thread_of）：
            不传则服务端新生成一个。
            图在指标反问处 interrupt 停住后，带同一个编号、把用户的回答作为 question 再调一次，即从停住的
            地方接着跑（见 nodes.ask_metric_definition）

        首次调用（不带 thread_id，或该编号下没有停住的运行）从意图识别开跑；带的是停住运行的编号时，
        这次输入被当作回答恢复执行（Command(resume=...)）。每帧都带上 thread_id，客户端据此把后一轮接回来。
        意图分析判定问题不适合查询时以错误步结束（不经 SQL 生成）；SQL 校验失败会回到 correct_sql
        重新生成，超过重试上限同样以错误步结束；正常路径的最后一步是 execute_sql，携带查询结果。

        耗时与 Token 用量统计挂在回调上（见 TimingHandler）：每个节点跑完记一条耗时，本轮结束再记一条汇总
        （各节点耗时、总耗时与 Token 用量）；中断、报错、客户端断开时也会在 finally 里补上汇总，不因异常丢掉
        统计。每帧还带上「到目前为止」的累计 Token 用量（``Step.token``，见下），最后一帧的 total 即本轮总量。
        """
        if self.graph is None:
            raise RuntimeError("检索图尚未装配：应用启动时应先调用 register()")
        thread = thread_id or str(Snowflake.get_id())
        config = {"configurable": {"thread_id": thread}}
        context = Context(model=model, embedding_model=bge_large_zh_embeddings, rerank_model=qwen_rerank)
        payload: Any = State(question=question, database_id=database_id)
        if thread_id:
            snapshot = await self.graph.aget_state(config)
            if snapshot.next:
                # 上一轮停在指标反问上：把这次输入当作回答，让图从停住的节点接着跑
                logger.info(f"invoke: resume thread {thread}")
                payload = Command(resume=question)
        logger.info(f"invoke: thread {thread}, database {database_id}, question={question!r}")
        # 回调只在跑图这一趟上挂：aget_state 之类的状态读取不必进统计
        timing = TimingHandler("retrieval")
        try:
            async for step in self.graph.astream(payload, context=context, stream_mode="custom",
                                                 config={**config, "callbacks": [timing]}):
                step.thread_id = thread
                # 每帧带上「到目前为止」的累计 Token 用量：最后一帧的 total 就是这一轮的总用量，
                # 调用方（检索接口）据此写进 Turn.tokens / Session.tokens
                # 注意字段名是 Step.token（帧上的键叫 token），不是 tokens——写错名字只会多挂一个
                # 未被声明的属性，asdict 不认，帧里就没有用量了
                step.token = dict(timing.tokens)
                yield step
        finally:
            timing.report()

    async def eval(self, question: str, database_id: int) -> tuple[str | None, list[TableInfo]]:
        """以评估模式跑完整条检索流程，返回校验无误的 SQL 与它检索到的表信息。

        database_id 同 invoke，指定本次检索的业务库编号。评估模式跳过意图识别、指标过滤与反问、SQL 的大模型
        合规校验，但会照常执行 SQL（确认它真能跑通）。
        结果帧以「SQL 与表片段同帧出现」为准：生成 / 校正出来的 SQL 只有 SQL、没有表片段，不算结果；
        校验通过那一帧与执行完成那一帧都满足，这里取最后出现的一次。
        表信息是重排后最终用于生成这条 SQL 的表、字段与取值，评估侧把它转成字典落库，作为这一轮检索到的
        片段（见 models.dataset.Result.retrieved_contexts）。

        走的是不带 checkpointer 的那张图（见类文档）：评估是批量的，没有人来回答反问，也不能把状态写进
        会话存储。
        """
        graph = self.eval_graph or self.build().compile(checkpointer=False)
        state = State(question=question, database_id=database_id, mode=Mode.EVALUATION)
        context = Context(model=model, embedding_model=bge_large_zh_embeddings, rerank_model=qwen_rerank)
        sql, table_infos = None, []
        async for step in graph.astream(state, context=context, stream_mode="custom"):
            # 结果帧：SQL 与表片段同时存在（校验通过帧、执行完成帧都算，后出现的覆盖前面的）
            if step.sql and step.table_infos:
                sql, table_infos = step.sql, step.table_infos
            elif step.error:
                logger.warning(f"eval: {step.error}")
        return sql, table_infos
