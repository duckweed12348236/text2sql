from contextlib import asynccontextmanager
from typing import AsyncGenerator

from models import Model
from plugins import register_plugins, close_plugins
# 导入 plugins.celery 即注册 Celery 插件（PluginMeta 在类定义时登记）：应用启动时由
# register_plugins 拉起 worker 子进程，关闭时由 close_plugins 停掉它
import plugins.celery
from plugins.redis import Task
from controllers import evaluation, retrieval, modeling
from config import CORS_ALLOW_ORIGINS, CORS_ALLOW_CREDENTIALS, CORS_ALLOW_METHODS, CORS_ALLOW_HEADERS, \
    CORS_EXPOSE_HEADERS
from logger import setup_logger, teardown_logger

from fastapi import FastAPI
from loguru import logger
import uvicorn
from starlette.middleware.cors import CORSMiddleware


@asynccontextmanager
async def register(app: FastAPI) -> AsyncGenerator[None, None]:
    setup_logger()
    logger.info("application startup")
    await Model.init()
    # 会话记忆（短期记忆组件）：这里开池并建表，关闭时把连接收掉——API 进程只拿它删数据集时忘记那一轮
    # （出题与评测都在 worker 进程里跑评估图，见 plugins.celery.bootstrap_worker）
    # 任务记录（Redis）：连接与索引一次备好，投递出题任务与轮询进度都要用（见 plugins.redis.Task）
    await Task.setup()
    await register_plugins()
    yield
    try:
        logger.info("application shutdown")
        await close_plugins()
        await evaluation.evaluator.close()
        await Model.close()
    finally:
        # 刷新 enqueue 队列，确保关闭阶段的日志全部落盘
        teardown_logger()


app = FastAPI(lifespan=register)
app.include_router(retrieval.router)
app.include_router(modeling.router)
app.include_router(evaluation.router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOW_ORIGINS,
    allow_credentials=CORS_ALLOW_CREDENTIALS,
    allow_methods=CORS_ALLOW_METHODS,
    allow_headers=CORS_ALLOW_HEADERS,
    expose_headers=CORS_EXPOSE_HEADERS
)

if __name__ == "__main__":
    # Windows 上必须显式给 uvicorn 指定 Selector 事件循环：短期记忆组件的 psycopg 异步模式不支持
    # 默认的 ProactorEventLoop（uvicorn 只在 --reload / --workers 子进程模式下才自己挑 Selector）。
    # 直接跑 uvicorn/fastapi 命令时同理：--loop plugins.agents.checkpointers:loop_factory
    uvicorn.run(app, loop="plugins.agents.checkpointers:loop_factory")
