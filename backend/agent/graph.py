from langgraph.graph import StateGraph, START, END
from agent.schemas import WSAgentState, WSAgentContext
from agent.nodes.add_extra_context import add_extra_context
from agent.nodes.correct_sql import correct_sql
from agent.nodes.execute_sql import execute_sql
from agent.nodes.extract_keywords import extract_keywords
from agent.nodes.filter_metrics import filter_metrics
from agent.nodes.filter_tables import filter_tables
from agent.nodes.generate_sql import generate_sql
from agent.nodes.merge_recalled_infos import merge_recalled_infos
from agent.nodes.parse_query_intention import parse_query_intention
from agent.nodes.recall_columns import recall_columns
from agent.nodes.recall_metrics import recall_metrics
from agent.nodes.recall_values import recall_values
from agent.nodes.validate_sql import validate_sql
from langchain.messages import HumanMessage

from clients.mysql import dw_mysql_client, meta_mysql_client
from clients.es import es_client
from clients.qdrant import qdrant_client
from clients.embedding import embedding_client

from repositories.mysql import DWDBRepository, MetaDBRepository
from repositories.es import ESRepository
from repositories.qdrant import ColumnQdrantRepository, MetricQdrantRepository


graph_builder = StateGraph(
    state_schema=WSAgentState,
    context_schema=WSAgentContext
)

# 添加节点
graph_builder.add_node(parse_query_intention)
graph_builder.add_node(extract_keywords)
graph_builder.add_node(recall_columns)
graph_builder.add_node(recall_metrics)
graph_builder.add_node(recall_values)
graph_builder.add_node(merge_recalled_infos)
graph_builder.add_node(filter_metrics)
graph_builder.add_node(filter_tables)
graph_builder.add_node(add_extra_context)
graph_builder.add_node(generate_sql)
graph_builder.add_node(validate_sql)
graph_builder.add_node(correct_sql)
graph_builder.add_node(execute_sql)

# 添加边
graph_builder.add_edge(START, "parse_query_intention")

graph_builder.add_conditional_edges(
    "parse_query_intention", 
    lambda state: "extract_keywords" if state.should_continue else END
)

graph_builder.add_edge("extract_keywords", "recall_columns")
graph_builder.add_edge("extract_keywords", "recall_metrics")
graph_builder.add_edge("extract_keywords", "recall_values")

graph_builder.add_edge("recall_columns", "merge_recalled_infos")
graph_builder.add_edge("recall_metrics", "merge_recalled_infos")
graph_builder.add_edge("recall_values", "merge_recalled_infos")

graph_builder.add_edge("merge_recalled_infos", "filter_metrics")
graph_builder.add_edge("merge_recalled_infos", "filter_tables")

graph_builder.add_edge("filter_metrics", "add_extra_context")
graph_builder.add_edge("filter_tables", "add_extra_context")

graph_builder.add_edge("add_extra_context", "generate_sql")

graph_builder.add_edge("generate_sql", "validate_sql")

graph_builder.add_conditional_edges(
    "validate_sql", 
    lambda state: "correct_sql" if state.error is not None else "execute_sql"
)

graph_builder.add_edge("correct_sql", "validate_sql")

graph_builder.add_edge("execute_sql", END)

graph = graph_builder.compile()


if __name__ == "__main__":
    import asyncio
    async def test():
        try:
            column_qdrant_repo = ColumnQdrantRepository(qdrant_client.client)
            metric_qdrant_repo = MetricQdrantRepository(qdrant_client.client)
            es_repo = ESRepository(es_client.client)
            async with (
                dw_mysql_client.session() as dw_session,
                meta_mysql_client.session() as meta_session
            ):
                dw_db_repo = DWDBRepository(dw_session)
                meta_db_repo = MetaDBRepository(meta_session)
                state = WSAgentState(messages=[HumanMessage(content="帮我统计一下上个季度华东地区销售额排名前三的产品")])
                context = WSAgentContext(
                    column_qdrant_repo=column_qdrant_repo,
                    metric_qdrant_repo=metric_qdrant_repo,
                    es_repo=es_repo,
                    meta_db_repo=meta_db_repo,
                    dw_db_repo=dw_db_repo
                )
                async for chunk in graph.astream(state, context=context, stream_mode="custom"):
                    print(chunk)
        finally:
            await qdrant_client.close()
            await es_client.close()
            await dw_mysql_client.close()
            await meta_mysql_client.close()
    asyncio.run(test())
    
    # from agent.prompts import load_prompt
    # import asyncio
    # async def test():
    #     prompt = await load_prompt("correct_sql")
    #     print(prompt)
    # asyncio.run(test())