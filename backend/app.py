from contextlib import asynccontextmanager
from typing import AsyncGenerator

from models import Model
from plugins import PluginManager
from controllers import chat
from config import CORS_ALLOW_ORIGINS, CORS_ALLOW_CREDENTIALS, CORS_ALLOW_METHODS, CORS_ALLOW_HEADERS, \
    CORS_EXPOSE_HEADERS

from fastapi import FastAPI
from loguru import logger
import uvicorn
from starlette.middleware.cors import CORSMiddleware


@asynccontextmanager
async def register_plugins(app: FastAPI) -> AsyncGenerator[None, None]:
    logger.add("logs/{time:YYYY-MM-DD}.log",
               rotation="00:00",
               encoding="utf-8",
               level="INFO",
               enqueue=True)
    await Model.init()
    await PluginManager.init()
    yield
    await PluginManager.close()
    await Model.close()


app = FastAPI(lifespan=register_plugins)
app.include_router(chat.router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOW_ORIGINS,
    allow_credentials=CORS_ALLOW_CREDENTIALS,
    allow_methods=CORS_ALLOW_METHODS,
    allow_headers=CORS_ALLOW_HEADERS,
    expose_headers=CORS_EXPOSE_HEADERS
)

if __name__ == "__main__":
    uvicorn.run(app)
