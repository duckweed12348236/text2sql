from agent.schemas import WSAgentState, WSAgentContext, WSStepInfo
from langgraph.runtime import Runtime
from agent.prompts import load_prompt
from typing import Any
from core.log import logger
from langchain_core.prompts import PromptTemplate
from langchain_core.output_parsers import JsonOutputParser
from agent.llm import llm
from dtos.meta import MetricInfo


async def filter_metrics(state: WSAgentState, runtime: Runtime[WSAgentContext]):
    # 实时反馈节点执行的状态
    writer = runtime.stream_writer
    writer(WSStepInfo(step="过滤指标信息", status="running"))

    query = state.messages[-1].content
    metric_infos: list[MetricInfo] = state.metric_infos or []

    # logger.info("过滤之前：")
    # logger.info([metric_info.name for metric_info in metric_infos])

    prompt = await load_prompt("filter_metric_info")
    prompt_template = PromptTemplate(template=prompt, input_variables=['query', 'metric_infos'])
    chain = prompt_template | llm | JsonOutputParser()
    result: dict[str, list[str]] = await chain.ainvoke(
        {
            "query": query, 
            "metric_infos": [metric_info.model_dump() for metric_info in metric_infos]
        }
    )

    for metric_info in metric_infos[:]:
        if metric_info.name not in result:
            metric_infos.remove(metric_info)
    
    writer(WSStepInfo(step="过滤指标信息", status="success"))

    # logger.info("过滤之后：")
    # logger.info([metric_info.name for metric_info in metric_infos])

    return {"metric_infos": metric_infos}