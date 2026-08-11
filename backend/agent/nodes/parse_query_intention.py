from agent.schemas import WSAgentState, WSAgentContext, WSStepInfo
from langgraph.runtime import Runtime
from pydantic import BaseModel
from agent.prompts import load_prompt
from agent.llm import llm
from langchain.messages import SystemMessage


class ParseQueryResult(BaseModel):
    should_continue: bool
    guide_queries: list[str]


async def parse_query_intention(state: WSAgentState, runtime: Runtime[WSAgentContext]):
    # 实时反馈节点执行的状态
    writer = runtime.stream_writer
    writer(WSStepInfo(step="解析提问意图", status="running"))
    try:
        prompt = await load_prompt("parse_query_intention")
        structured_llm = llm.with_structured_output(ParseQueryResult, method="json_mode")
        result: ParseQueryResult = await structured_llm.ainvoke([
            SystemMessage(content=prompt)
        ] + state.messages)
        # writer(WSStepInfo(step="解析提问意图", status="success"))
        # print(result)
        # return {"should_continue": result.should_continue, "guide_queries": result.guide_queries}
        if result.should_continue:
            writer(WSStepInfo(step="解析提问意图", status="success"))
            return {"should_continue": result.should_continue}
        else:
            writer(WSStepInfo(step="解析提问意图", status="success", finish=True, guide_queries=result.guide_queries))
            return {"should_continue": result.should_continue}
    except Exception as e:
        writer(WSStepInfo(step="解析提问意图", status="error"))
        return {"should_continue": False, "error": str(e)}