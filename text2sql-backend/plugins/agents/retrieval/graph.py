from typing import Any, AsyncGenerator

from langgraph.graph import StateGraph
from loguru import logger

from plugins import Singleton
from plugins.agents.models import bge_large_zh_embeddings, model, qwen_rerank
from plugins.agents.retrieval.nodes import analyse_intention, extract_keywords, recall_columns, recall_metrics, \
    recall_values, filter_tables, filter_metrics, rerank, add_extra_info, generate_sql, validate_sql, \
    correct_sql, execute_sql
from plugins.agents.retrieval.schemas import Context, Mode, State, Step, TableInfo


class Retriever(Singleton):
    """检索图的入口：把用户的自然语言问题转成 SQL 并执行。

    analyse_intention → extract_keywords → recall_columns → recall_metrics → recall_values → rerank →
    add_extra_info → generate_sql → validate_sql → execute_sql → 结束：节点之间的跳转写在节点内部
    （Command(goto=...)），与采集图同一套写法，这里只负责注册节点、指定入口与终点并编译。
    filter_tables 与 filter_metrics 暂不在链路上（recall_values 直接跳到 rerank），节点仍注册在图里，
    恢复时把 recall_values 的 goto 改回 filter_tables 即可（filter_tables → filter_metrics → rerank）。
    评估模式下 analyse_intention 不调用模型、直接跳到
    extract_keywords，相当于跳过意图识别；SQL 校验通过后也直接结束（跳过 execute_sql），只输出生成的 SQL。
    校验失败时由节点自行回到 correct_sql 重新生成（重试上限见 nodes.MAX_RETRY）。
    LLM、嵌入模型与重排模型取自 plugins.agents.models，运行时以 Context 注入，节点里从
    runtime.context 取用；检索哪个库、哪种模式属于这一轮的输入，随初始状态交给图
    （见 State.database_id / State.mode），节点里从 state 取用。

    本图不落 checkpoint（compile 时显式 checkpointer=False）：评测节点里会把它当子图跑
    （见 plugins.agents.evaluation.nodes.evaluate），而 langgraph 会把父图（评估图）的 checkpointer
    顺着 config 传给被嵌套调用的图——检索状态不是要保存的东西（一轮检索跑完即弃），并发评测同一批
    问题时还会撞进同一个命名空间互相覆盖；更麻烦的是它的状态类型不在评估图 serde 的允许清单里，
    再读回来会被拒（见 plugins.agents.evaluation.graph.Evaluator.register）。
    """

    def __init__(self):
        graph_builder = StateGraph(State, context_schema=Context)
        graph_builder.add_node(analyse_intention)
        graph_builder.add_node(extract_keywords)
        graph_builder.add_node(recall_columns)
        graph_builder.add_node(recall_metrics)
        graph_builder.add_node(recall_values)
        # filter_tables 与 filter_metrics 暂时不接入链路（注册着以便随时恢复，见类文档）
        graph_builder.add_node(filter_tables)
        graph_builder.add_node(filter_metrics)
        graph_builder.add_node(rerank)
        graph_builder.add_node(add_extra_info)
        graph_builder.add_node(generate_sql)
        graph_builder.add_node(validate_sql)
        graph_builder.add_node(correct_sql)
        graph_builder.add_node(execute_sql)
        graph_builder.set_entry_point("analyse_intention")
        graph_builder.set_finish_point("execute_sql")
        # checkpointer=False：显式声明本图不落 checkpoint，别让父图的 checkpointer 顺下来了（见类文档）
        self.graph = graph_builder.compile(checkpointer=False)

    async def invoke(self, question: str, database_id: int) -> AsyncGenerator[Step, Any]:
        """逐条产出检索进度（SSE 数据帧，与采集图的输出格式一致）。

        database_id 指定本次检索的业务库编号（Database.id）：字段与取值都在该库的向量集合里召回；
        意图分析判定问题不适合查询时以错误步结束（不经 SQL 生成）；SQL 校验失败会回到 correct_sql
        重新生成，超过重试上限同样以错误步结束；正常路径的最后一步是 execute_sql，携带查询结果。
        """
        state = State(question=question, database_id=database_id)
        context = Context(model=model, embedding_model=bge_large_zh_embeddings, rerank_model=qwen_rerank)
        async for step in self.graph.astream(state, context=context, stream_mode="custom"):
            yield step

    async def eval(self, question: str, database_id: int) -> tuple[str | None, list[TableInfo]]:
        """以评估模式跑完整条检索流程，返回校验无误的 SQL 与它检索到的表信息。

        database_id 同 invoke，指定本次检索的业务库编号。评估模式跳过意图识别、SQL 校验不调用大模型，
        且校验通过后不执行 SQL，故跑到最后是校验通过那一帧（见 nodes.validate_sql）。
        结果帧以「SQL 与表片段同帧出现」为准：生成 / 校正出来的 SQL 只有 SQL、没有表片段，不算结果；
        校验没通过时根本到不了结果帧。故这里只需判断两者是否同时存在，取到即整轮的结果，取不到就都为空。
        表信息是重排后最终用于生成这条 SQL 的表、字段与取值，评估侧把它转成字典落库，作为这一轮检索到的
        片段（见 models.dataset.Result.retrieved_contexts）。
        """
        state = State(question=question, database_id=database_id, mode=Mode.EVALUATION)
        context = Context(model=model, embedding_model=bge_large_zh_embeddings, rerank_model=qwen_rerank)
        sql, table_infos = None, []
        async for step in self.graph.astream(state, context=context, stream_mode="custom"):
            # 结果帧：SQL 与表片段同时存在（评估模式为校验通过那一帧，见 nodes.validate_sql）
            if step.sql and step.table_infos:
                sql, table_infos = step.sql, step.table_infos
            elif step.error:
                logger.warning(f"eval: {step.error}")
        return sql, table_infos
