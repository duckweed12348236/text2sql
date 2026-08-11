from agent.schemas import WSAgentState, WSAgentContext, WSStepInfo
from langgraph.runtime import Runtime
from datetime import datetime
from core.log import logger


async def add_extra_context(state: WSAgentState, runtime: Runtime[WSAgentContext]):
    # 实时反馈节点执行的状态
    writer = runtime.stream_writer
    writer(WSStepInfo(step="添加额外信息", status="running"))

    # 1. 获取数据库信息
    dw_db_repo = runtime.context.dw_db_repo

    db_infos = await dw_db_repo.get_db_info()
    db_info = f"数据库类型：{db_infos['dialect']}，编码方式：{db_infos['charset']}，版本号：{db_infos['version']}"

    # 2. 获取当前的时间信息
    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    weekday = now.weekday() + 1
    quarter = f"Q{(now.month-1) // 3 + 1}"
    date_info = f"当前的时间：{now_str}，星期：{weekday}，季度：{quarter}"

    # logger.info(db_info)
    # logger.info(date_info)

    writer(WSStepInfo(step="添加额外信息", status="success"))

    return {"db_info": db_info, "date_info": date_info}