from agent.schemas import WSAgentState, WSAgentContext, WSStepInfo
from langgraph.runtime import Runtime
from agent.prompts import load_prompt
from typing import Any
from core.log import logger
from langchain_core.prompts import PromptTemplate
from langchain_core.output_parsers import JsonOutputParser
from agent.llm import llm
from agent.schemas import WSAgentTableInfoState


async def filter_tables(state: WSAgentState, runtime: Runtime[WSAgentContext]):
    # 实时反馈节点执行的状态
    writer = runtime.stream_writer
    writer(WSStepInfo(step="过滤表结构信息", status="running"))

    query = state.messages[-1].content
    table_infos: list[WSAgentTableInfoState] = state.table_infos or []

    # logger.info("过滤之前：")
    # logger.info([
    #     (table_info.name, [column_info.name for column_info in table_info.columns])
    #     for table_info in table_infos
    # ])

    prompt = await load_prompt("filter_table_info")
    prompt_template = PromptTemplate(template=prompt, input_variables=['query', 'table_infos'])
    chain = prompt_template | llm | JsonOutputParser()
    result: dict[str, list[str]] = await chain.ainvoke(
        {
            "query": query, 
            "table_infos": [table_info.model_dump() for table_info in table_infos]
        }
    )

    for table_info in table_infos[:]:
        if table_info.name not in result:
            table_infos.remove(table_info)
            continue
        for column_info in table_info.columns[:]:
            if column_info.name not in result[table_info.name]:
                table_info.columns.remove(column_info)
    
    writer(WSStepInfo(step="过滤表结构信息", status="success"))

    # logger.info("过滤之后：")
    # logger.info([
    #     (table_info.name, [column_info.name for column_info in table_info.columns])
    #     for table_info in table_infos
    # ])

    return {"table_infos": table_infos}


