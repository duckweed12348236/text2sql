from typing import Any, AsyncGenerator

from langgraph.graph import StateGraph

from plugins import Singleton
from plugins.agents.modeling.nodes import discover_tables, embed_columns
from plugins.agents.modeling.schemas import Context, State, Step
from plugins.agents.models import bge_large_zh_embeddings, model


class Modeler(Singleton):
    """采集图的入口：把业务库的表、列结构同步到元数据库与向量库。

    discover_tables → embed_columns → 结束：节点之间的跳转写在节点内部（Command(goto=...)），
    与检索图同一套写法，这里只负责注册节点、指定入口与终点并编译。
    LLM 与嵌入模型取自 plugins.agents.models，运行时以 Context 注入，节点里从 runtime.context 取用；
    采哪个库属于这一轮的输入，随初始状态交给图（见 State.database_id）。
    """

    def __init__(self):
        graph_builder = StateGraph(State, context_schema=Context)
        graph_builder.add_node(discover_tables)
        graph_builder.add_node(embed_columns)
        graph_builder.set_entry_point("discover_tables")
        graph_builder.set_finish_point("embed_columns")
        self.graph = graph_builder.compile()

    async def invoke(self,
                     table_ids: list[str] | None = None,
                     database_id: int | None = None) -> AsyncGenerator[Step, Any]:
        """逐条产出采集进度（SSE 数据帧，与检索图的输出格式一致）。

        :param table_ids: 本轮采集的表（元数据库里的表编号，形如 <Database.id>.<表名>）：采集按编号找到
            业务库表名、读结构并覆盖元数据库里的旧数据，随后向量化这些表的字段并采集其取值；已登记的编号
            再次指定即为重采（覆盖）。编号全局唯一，跨库同名表不会串台；未登记的编号按首次采集处理，
            从其前缀里取出表名。留空表示不限定范围：本库（database_id）的全部表
        :param database_id: 这些表所属的业务库编号（Database.id）：表编号以它作前缀，写入的
            Table.database_id 就是它。Table.database_id 是必填列、表编号也要靠它拼，所以走采集
            （本方法）时必须带上它，否则采集节点直接报错结束、不写库；只有单独调 embed_columns
            且给了 table_ids 时可以留空
        """
        async for step in self.graph.astream(
                State(table_ids=table_ids or [], database_id=database_id),
                context=Context(model=model, embedding_model=bge_large_zh_embeddings),
                stream_mode="custom"):
            yield step
