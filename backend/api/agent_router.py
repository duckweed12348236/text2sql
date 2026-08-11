from fastapi import APIRouter
from api.schemas import QueryInput
from fastapi.responses import StreamingResponse
from langchain.messages import HumanMessage

from clients.mysql import dw_mysql_client, meta_mysql_client
from clients.es import es_client
from clients.qdrant import qdrant_client
from clients.embedding import embedding_client

from repositories.mysql import DWDBRepository, MetaDBRepository
from repositories.es import ESRepository
from repositories.qdrant import ColumnQdrantRepository, MetricQdrantRepository

from agent.schemas import WSAgentState, WSAgentContext

from agent.graph import graph


router = APIRouter(prefix="/agent")


async def query_graph(query: str):
    column_qdrant_repo = ColumnQdrantRepository(qdrant_client.client)
    metric_qdrant_repo = MetricQdrantRepository(qdrant_client.client)
    es_repo = ESRepository(es_client.client)
    async with (
        dw_mysql_client.session() as dw_session,
        meta_mysql_client.session() as meta_session
    ):
        dw_db_repo = DWDBRepository(dw_session)
        meta_db_repo = MetaDBRepository(meta_session)
        state = WSAgentState(messages=[HumanMessage(content=query)])
        context = WSAgentContext(
            column_qdrant_repo=column_qdrant_repo,
            metric_qdrant_repo=metric_qdrant_repo,
            es_repo=es_repo,
            meta_db_repo=meta_db_repo,
            dw_db_repo=dw_db_repo
        )
        async for chunk in graph.astream(state, context=context, stream_mode="custom"):
            yield f"data: {chunk.model_dump_json()}\n\n"


# /agent/query
@router.post("/query")
async def query(data: QueryInput):
    query = data.query
    return StreamingResponse(query_graph(query), media_type="text/event-stream")