from typing import Any, AsyncGenerator

from langchain_core.messages import HumanMessage
from langgraph.graph import StateGraph, START, END

from plugins import Singleton
from plugins.agent.schemas import State, Step
from plugins.agent.nodes import analyse_intention, extract_keywords, recall_columns, recall_metrics, recall_values, \
    merge_infos, filter_tables, filter_metrics, add_extra_info, generate_sql, validate_sql, correct_sql, execute_sql


class Agent(Singleton):
    def __init__(self):
        graph_builder = StateGraph(State)
        graph_builder.add_node(analyse_intention)
        graph_builder.add_node(extract_keywords)
        graph_builder.add_node(recall_columns)
        graph_builder.add_node(recall_metrics)
        graph_builder.add_node(recall_values)
        graph_builder.add_node(merge_infos)
        graph_builder.add_node(filter_tables)
        graph_builder.add_node(filter_metrics)
        graph_builder.add_node(add_extra_info)
        graph_builder.add_node(generate_sql)
        graph_builder.add_node(validate_sql)
        graph_builder.add_node(correct_sql)
        graph_builder.add_node(execute_sql)
        graph_builder.set_entry_point(analyse_intention.key)
        graph_builder.set_finish_point(execute_sql.key)
        self.graph = graph_builder.compile()

    async def invoke(self, question: str) -> AsyncGenerator[str, Any]:
        state = State(messages=[HumanMessage(content=question)])
        async for step in self.graph.astream(state, stream_mode="custom"):
            yield f"data: {step.model_dump_json()}\n\n"
