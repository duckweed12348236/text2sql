from agent.schemas import WSAgentState, WSAgentContext, WSStepInfo
from langgraph.runtime import Runtime
from agent.prompts import load_prompt
from langchain_core.prompts import PromptTemplate
from langchain_core.output_parsers import StrOutputParser
from agent.llm import llm
from core.log import logger


async def correct_sql(state: WSAgentState, runtime: Runtime[WSAgentContext]):
    # 实时反馈节点执行的状态
    writer = runtime.stream_writer
    writer(WSStepInfo(step="校正SQL", status="running"))

    sql = state.sql
    error = state.error
    table_infos = state.table_infos
    metric_infos = state.metric_infos
    db_info = state.db_info
    date_info = state.date_info
    query = state.messages[-1].content

    prompt = await load_prompt("correct_sql")
    prompt_template = PromptTemplate(
        template=prompt, 
        input_variables=['query', 'error', 'table_infos', 'metric_infos', 'db_info', "date_info", "sql"]
    )
    chain = prompt_template | llm | StrOutputParser()

    result = await chain.ainvoke({
        "query": query,
        "sql": sql,
        "error": error,
        "table_infos": table_infos,
        "metric_infos": metric_infos,
        "db_info": db_info,
        "date_info": date_info
    })

    writer(WSStepInfo(step="校正SQL", status="success"))

    logger.info(f"校正后的SQL：{result}")

    return {"sql": result}