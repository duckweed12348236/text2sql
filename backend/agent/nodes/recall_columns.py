from agent.schemas import WSAgentState, WSAgentContext, WSStepInfo
from langgraph.runtime import Runtime
from agent.prompts import load_prompt
from langchain_core.prompts import PromptTemplate
from langchain_core.output_parsers import JsonOutputParser
from agent.llm import llm
from clients.embedding import embedding_client
from dtos.meta import ColumnInfo
from typing import Any
from core.log import logger


async def recall_columns(state: WSAgentState, runtime: Runtime[WSAgentContext]):
    # 实时反馈节点执行的状态
    writer = runtime.stream_writer
    writer(WSStepInfo(step="召回字段信息", status="running"))

    query = state.messages[-1].content

    keywords: list[str] = state.keywords

    context = runtime.context
    column_qdrant_repo = context.column_qdrant_repo

    # 1. 利用大模型拓展关键词
    prompt = await load_prompt("extend_keywords_for_column_recall")
    prompt_template = PromptTemplate(template=prompt, input_variables=['query'])
    chain = prompt_template | llm | JsonOutputParser()
    result = await chain.ainvoke({"query": query})
    keywords = list(set((keywords or []) + result))

    # logger.info(keywords)

    # 2. 利用关键词在qdrant中搜索相关的字段信息
    recalled_columns_mapping: dict[str, Any] = {}
    for keyword in keywords:
        embedding = await embedding_client.client.aembed_query(keyword)
        column_infos: list[ColumnInfo] = await column_qdrant_repo.search(embedding)
        for column_info in column_infos:
            if column_info.id not in recalled_columns_mapping:
                recalled_columns_mapping[column_info.id] = column_info
    
    recalled_columns: list[ColumnInfo] = list(recalled_columns_mapping.values())
    # logger.info(recalled_columns)

    writer(WSStepInfo(step="召回字段信息", status="success"))

    return {'recalled_columns': recalled_columns}
