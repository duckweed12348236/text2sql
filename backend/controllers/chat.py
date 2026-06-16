from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from plugins.agent import Agent
from schemas.chat import Question

router = APIRouter(prefix="/chat")
agent = Agent()


@router.post("")
async def post_question(question: Question):
    return StreamingResponse(agent.invoke(question.content), media_type="text/event-stream")