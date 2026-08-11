from contextlib import asynccontextmanager
from fastapi import FastAPI
from clients.qdrant import qdrant_client
from clients.es import es_client
from clients.mysql import dw_mysql_client, meta_mysql_client


@asynccontextmanager
async def lifespan(_: FastAPI):
    
    yield
    # FastAPI 应用结束前执行
    await qdrant_client.close()
    await es_client.close()
    await dw_mysql_client.close()
    await meta_mysql_client.close()