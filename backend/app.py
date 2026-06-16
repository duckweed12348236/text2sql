from contextlib import asynccontextmanager
from typing import AsyncGenerator

from models import Model
from plugins import PluginManager
from controllers import chat

from fastapi import FastAPI
from loguru import logger


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

import middlewares
