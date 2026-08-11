from agent.schemas import WSAgentState, WSAgentContext, WSStepInfo
from langgraph.runtime import Runtime
from agent.prompts import load_prompt
from typing import Any
from core.log import logger
from langchain_core.prompts import PromptTemplate
from langchain_core.output_parsers import JsonOutputParser
from agent.llm import llm
from dtos.es import ValueInfo


async def recall_values(state: WSAgentState, runtime: Runtime[WSAgentContext]):
    # 实时反馈节点执行的状态
    writer = runtime.stream_writer
    writer(WSStepInfo(step="召回业务数据", status="running"))

    query = state.messages[-1].content

    keywords: list[str] = state.keywords

    context = runtime.context
    es_repo = context.es_repo

    # 1. 利用大模型拓展关键词
    prompt = await load_prompt("extend_keywords_for_value_recall")
    prompt_template = PromptTemplate(template=prompt, input_variables=['query'])
    chain = prompt_template | llm | JsonOutputParser()
    result = await chain.ainvoke({"query": query})
    keywords = list(set((keywords or []) + result))

    # logger.info(keywords)

    # 2. 利用关键词在es中搜索相关的值信息
    recalled_values_mapping: dict[str, Any] = {}
    for keyword in keywords:
        value_infos: list[ValueInfo] = await es_repo.search(keyword)
        for value_info in value_infos:
            if value_info.id not in recalled_values_mapping:
                recalled_values_mapping[value_info.id] = value_info
    
    recalled_values: list[ValueInfo] = list(recalled_values_mapping.values())
    # logger.info(recalled_values)

    writer(WSStepInfo(step="召回业务数据", status="success"))

    return {'recalled_values': recalled_values}