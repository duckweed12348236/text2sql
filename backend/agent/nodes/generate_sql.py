from agent.schemas import WSAgentState, WSAgentContext, WSStepInfo
from langgraph.runtime import Runtime
from agent.prompts import load_prompt
from langchain_core.prompts import PromptTemplate
from langchain_core.output_parsers import StrOutputParser
from agent.llm import llm
from core.log import logger

async def generate_sql(state: WSAgentState, runtime: Runtime[WSAgentContext]):
    # 实时反馈节点执行的状态
    writer = runtime.stream_writer
    writer(WSStepInfo(step="生成SQL", status="running"))

    query = state.messages[-1].content
    table_infos = state.table_infos
    metric_infos = state.metric_infos
    db_info = state.db_info
    date_info = state.date_info

    prompt = await load_prompt("generate_sql")
    prompt_template = PromptTemplate(template=prompt, input_variables=['query', 'table_infos', 'metric_infos', 'db_info', 'date_info'])
    chain = prompt_template | llm | StrOutputParser()

    result = await chain.ainvoke({
        "query": query,
        "table_infos": [table_info.model_dump() for table_info in table_infos],
        "metric_infos": [metric_info.model_dump() for metric_info in metric_infos],
        "db_info": db_info,
        "date_info": date_info
    })

    # 为了测试校正SQL节点的执行才故意写一个有问题的SQL
    # result = "select * from table_abc"

    writer(WSStepInfo(step="生成SQL", status="success"))

    logger.info(f"sql: {result}")

    return {"sql": result}