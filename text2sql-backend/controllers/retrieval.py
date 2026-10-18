from dataclasses import asdict
from typing import Any, AsyncGenerator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from plugins.agents.retrieval.graph import Retriever
from plugins.json import dump
from schemas.retrieval import Question

router = APIRouter(prefix="/retrieval")
retriever = Retriever()


async def output(content: str, database_id: int) -> AsyncGenerator[str, Any]:
    """把检索进度包成 SSE 数据帧：节点每上报一条 Step，就推一帧 `data: {json}\\n\\n`。"""
    async for step in retriever.invoke(content, database_id):
        yield f"data: {dump(asdict(step), default=str)}\n\n"


@router.post("")
async def submit_question(question: Question):
    """把自然语言问题转成 SQL 并执行：逐条产出检索进度（SSE 数据帧，与采集图的输出格式一致）。

    content 为待查询的自然语言问题；database_id 指定本次检索的业务库编号（元数据库里的 Database.id），
    字段与取值都只在该库的向量集合里召回，不跨库。问题不适合查询、SQL 校验未通过或执行报错，
    都以携带 error 的错误帧结束，不抛出异常。
    """
    return StreamingResponse(
        output(question.content, question.database_id),
        media_type="text/event-stream"
    )
