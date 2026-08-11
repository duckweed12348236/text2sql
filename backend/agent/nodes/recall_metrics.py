from agent.schemas import WSAgentState, WSAgentContext, WSStepInfo
from langgraph.runtime import Runtime
from dtos.meta import MetricInfo
from agent.prompts import load_prompt
from typing import Any
from core.log import logger
from langchain_core.prompts import PromptTemplate
from langchain_core.output_parsers import JsonOutputParser
from agent.llm import llm
from clients.embedding import embedding_client


async def recall_metrics(state: WSAgentState, runtime: Runtime[WSAgentContext]):
    # 实时反馈节点执行的状态
    writer = runtime.stream_writer
    writer(WSStepInfo(step="召回指标信息", status="running"))

    query = state.messages[-1].content

    keywords: list[str] = state.keywords

    context = runtime.context
    metric_qdrant_repo = context.metric_qdrant_repo

    # 1. 利用大模型拓展关键词
    prompt = await load_prompt("extend_keywords_for_metric_recall")
    prompt_template = PromptTemplate(template=prompt, input_variables=['query'])
    chain = prompt_template | llm | JsonOutputParser()
    result = await chain.ainvoke({"query": query})
    keywords = list(set((keywords or []) + result))

    # logger.info(keywords)

    # 2. 利用关键词在qdrant中搜索相关的字段信息
    recalled_metrics_mapping: dict[str, Any] = {}
    for keyword in keywords:
        embedding = await embedding_client.client.aembed_query(keyword)
        metric_infos: list[MetricInfo] = await metric_qdrant_repo.search(embedding)
        for metric_info in metric_infos:
            if metric_info.id not in recalled_metrics_mapping:
                recalled_metrics_mapping[metric_info.id] = metric_info
    
    recalled_metrics: list[MetricInfo] = list(recalled_metrics_mapping.values())
    # logger.info(recalled_metrics)

    writer(WSStepInfo(step="召回指标信息", status="success"))

    return {'recalled_metrics': recalled_metrics}