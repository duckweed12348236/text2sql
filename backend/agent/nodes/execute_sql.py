from agent.schemas import WSAgentState, WSAgentContext, WSStepInfo
from langgraph.runtime import Runtime
from core.log import logger


async def execute_sql(state: WSAgentState, runtime: Runtime[WSAgentContext]):
    # 实时反馈节点执行的状态
    writer = runtime.stream_writer
    writer(WSStepInfo(step="执行SQL", status="running"))

    sql = state.sql or ""
    dw_db_repo = runtime.context.dw_db_repo

    result = await dw_db_repo.execute_sql(sql)

    logger.info(f"sql执行后的结果：{result}")

    writer(WSStepInfo(step="执行SQL", status="success",data=result, finish=True))