from agent.schemas import WSAgentState, WSAgentContext, WSStepInfo
from langgraph.runtime import Runtime
from core.log import logger


async def validate_sql(state: WSAgentState, runtime: Runtime[WSAgentContext]):
    # 实时反馈节点执行的状态
    writer = runtime.stream_writer
    writer(WSStepInfo(step="校验SQL", status="running"))

    dw_db_repo = runtime.context.dw_db_repo

    sql = state.sql

    try:
        await dw_db_repo.validate_sql(sql)
        writer(WSStepInfo(step="校验SQL", status="success"))
        logger.info("SQL校验成功！")
        return {"error": None}
    except Exception as e:
        writer(WSStepInfo(step="校验SQL", status="error"))
        logger.info("SQL校验失败！")
        return {"error": str(e)}