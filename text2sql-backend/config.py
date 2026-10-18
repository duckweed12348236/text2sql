import tomllib
from pathlib import Path
from pkgutil import iter_modules

# 日志参数（由 logger.setup_logger 使用）
LOG_LEVEL = "INFO"
LOG_FILE = "logs/{time:YYYY-MM-DD}.log"
# Celery worker 进程的日志单独成文件（由 logger.setup_celery_logger 使用），不与应用日志混在一起
LOG_CELERY_FILE = "logs/{time:YYYY-MM-DD}.celery.log"
LOG_ROTATION = "00:00"
LOG_RETENTION = "30 days"
LOG_CONSOLE_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
    "<level>{message}</level>"
)
LOG_FILE_FORMAT = "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message}"


def _load_config(section: str, key: str, default=None):
    """读取 [tool.<section>] 下的配置项。"""
    return PYPROJECT.get(section, {}).get(key, default)


# MySQL 连接串：元数据库搬到 PostgreSQL 之后，它只用来访问搬走之前的老元数据库（迁移脚本用），
# 业务库的连接地址在 database 表的行里、与它无关
MYSQL_URL = "mysql://root:123456@localhost:3306/text2sql"
# 元数据库走 PostgreSQL：业务库登记、表、字段、指标与评测数据集都在这个库里（评测会话的 checkpoint
# 也存在这里，见 plugins.agents.checkpointers）。scheme 用 postgres://：Tortoise 只认 asyncpg://
# 与它的别名 postgres://（不认 postgresql://），而 psycopg 认 postgres://，两边共用这一个值
POSTGRESQL_URL = "postgres://postgres:123456@localhost:5432/text2sql"

CORS_ALLOW_ORIGINS = ["*"]
CORS_ALLOW_CREDENTIALS = True
CORS_ALLOW_METHODS = ["*"]
CORS_ALLOW_HEADERS = ["*"]
CORS_EXPOSE_HEADERS = ["*"]

import models

TORTOISE_ORM_CONFIG = {
    "connections": {
        "meta": POSTGRESQL_URL
    },
    "apps": {
        "models": {
            "models": [f"{models.__name__}.{x.name}" for x in iter_modules(models.__path__)] + ["aerich.models"],
            "default_connection": "meta"
        }
    },
    "use_tz": True,
    "timezone": "UTC"
}

SNOWFLAKE_DATACENTER_ID = 0
SNOWFLAKE_WORKER_ID = 0
SNOWFLAKE_SEQUENCE = 0

# 大模型走火山方舟（Ark）的 OpenAI 兼容端点
DOUBAO_MODEL_KEY = ""
DOUBAO_MODEL_URL = "https://ark.cn-beijing.volces.com/api/v3"
DOUBAO_MODEL_NAME = "doubao-seed-2-1-pro-260915"

DEEPSEEK_MODEL_KEY = ""
DEEPSEEK_MODEL_URL = "https://ark.cn-beijing.volces.com/api/v3"
DEEPSEEK_MODEL_NAME = "deepseek-v4-1-flash-260910"

QDRANT_URL = "http://localhost:6333"

# Redis：检索索引、短期记忆缓存与 Celery 共用一套连接配置（连接串由 plugins/redis.py 的
# build_redis_url 拼出）。0 号库留给 RediSearch 索引与缓存（索引只允许建在 0 号库，见
# plugins.redis.migrate），故 Celery 的 broker 与结果后端另用两个库，互不打扰。
# 密码为空表示无鉴权。
REDIS_HOST = "localhost"
REDIS_PORT = 6379
REDIS_PASSWORD = ""
REDIS_DB = 0

# Celery：broker 与结果后端复用上面的 Redis 地址与口令，这里只指定各自的库号。
# concurrency 为 0、pool 为空串时用默认值（进程池在 Windows 上默认 solo，其余平台 prefork）。
CELERY_BROKER_DB = 1
CELERY_RESULT_DB = 2
CELERY_LOGLEVEL = "INFO"
CELERY_CONCURRENCY = 0
CELERY_POOL = ""
# 拉起 worker 时是否内嵌 beat（-B）按周期投递定时任务：单实例部署够用，多实例时应关掉并单独跑 celery beat
CELERY_BEAT = True
# 表结构巡检周期（秒）：定时收集业务库里已变动 / 新增 / 删除的表（见 plugins.celery.tasks）
CELERY_SCAN_INTERVAL = 600
# beat 的调度状态文件（PersistentScheduler 的 shelve 库）：放 logs 下，不在项目根目录留 celerybeat-schedule
CELERY_BEAT_SCHEDULE_FILE = "logs/celerybeat-schedule"

EMBEDDING_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
EMBEDDING_MODEL = "text-embedding-v4"
EMBEDDING_MODEL_KEY = ""

BGE_LARGE_ZH_MODEL_URL = "https://api.siliconflow.cn/v1"
BGE_LARGE_ZH_MODEL_NAME = "BAAI/bge-large-zh-v1.5"
BGE_LARGE_ZH_MODEL_KEY = ""

# 重排走硅基流动的 OpenAI 兼容端点（与 bge-large-zh 同一控制台）。OpenAI 官方协议没有重排端点，
# 各服务商路径不统一，故 QWEN_RERANK_URL 存完整请求地址（硅基流动为 {base}/v1/rerank）。
QWEN_RERANK_URL = "https://api.siliconflow.cn/v1/rerank"
QWEN_RERANK_MODEL = "Qwen/Qwen3-Reranker-8B"
QWEN_RERANK_MODEL_KEY = ""
# 任务说明（instruction）的字段名：各服务商叫法不统一（硅基流动为 instruction，百炼为 instruct），
# 不支持的端点会忽略该字段；置空则不发送。
QWEN_RERANK_INSTRUCT_FIELD = "instruction"

# 备选重排模型：硅基流动托管的 bge-reranker-v2-m3，完整请求地址为 {base}/v1/rerank。
# 目前仅登记配置，默认使用的仍是上面的 Qwen3-Reranker-8B；切换时改 plugins/agents/models.py 里的 reranker。
BGE_RERANKER_V2_M3_MODEL_URL = "https://api.siliconflow.cn/v1/rerank"
BGE_RERANKER_V2_M3_MODEL_NAME = "BAAI/bge-reranker-v2-m3"
BGE_RERANKER_V2_M3_MODEL_KEY = ""

READY = False

if not READY:
    READY = True

    # 从 pyproject.toml 的 [tool] 读取配置，未配置的项沿用上面的默认值
    with (Path(__file__).resolve().parent / "pyproject.toml").open("rb") as fp:
        PYPROJECT = tomllib.load(fp).get("tool", {})

    MYSQL_URL = _load_config("mysql", "url", MYSQL_URL)

    POSTGRESQL_URL = _load_config("postgresql", "url", POSTGRESQL_URL)
    TORTOISE_ORM_CONFIG["connections"]["meta"] = POSTGRESQL_URL

    CORS_ALLOW_ORIGINS = _load_config("cors", "allow_origins", CORS_ALLOW_ORIGINS)
    CORS_ALLOW_CREDENTIALS = _load_config("cors", "allow_credentials", CORS_ALLOW_CREDENTIALS)
    CORS_ALLOW_METHODS = _load_config("cors", "allow_methods", CORS_ALLOW_METHODS)
    CORS_ALLOW_HEADERS = _load_config("cors", "allow_headers", CORS_ALLOW_HEADERS)
    CORS_EXPOSE_HEADERS = _load_config("cors", "expose_headers", CORS_EXPOSE_HEADERS)

    SNOWFLAKE_DATACENTER_ID = _load_config("snowflake", "datacenter_id", SNOWFLAKE_DATACENTER_ID)
    SNOWFLAKE_WORKER_ID = _load_config("snowflake", "worker_id", SNOWFLAKE_WORKER_ID)
    SNOWFLAKE_SEQUENCE = _load_config("snowflake", "sequence", SNOWFLAKE_SEQUENCE)

    DOUBAO_MODEL_KEY = _load_config("doubao", "key", DOUBAO_MODEL_KEY)
    DOUBAO_MODEL_URL = _load_config("doubao", "url", DOUBAO_MODEL_URL)
    DOUBAO_MODEL_NAME = _load_config("doubao", "name", DOUBAO_MODEL_NAME)

    DEEPSEEK_MODEL_KEY = _load_config("deepseek", "key", DEEPSEEK_MODEL_KEY)
    DEEPSEEK_MODEL_URL = _load_config("deepseek", "url", DEEPSEEK_MODEL_URL)
    DEEPSEEK_MODEL_NAME = _load_config("deepseek", "name", DEEPSEEK_MODEL_NAME)

    QDRANT_URL = _load_config("qdrant", "url", QDRANT_URL)

    REDIS_HOST = _load_config("redis", "host", REDIS_HOST)
    REDIS_PORT = _load_config("redis", "port", REDIS_PORT)
    REDIS_PASSWORD = _load_config("redis", "password", REDIS_PASSWORD)
    REDIS_DB = _load_config("redis", "db", REDIS_DB)

    CELERY_BROKER_DB = _load_config("celery", "broker_db", CELERY_BROKER_DB)
    CELERY_RESULT_DB = _load_config("celery", "result_db", CELERY_RESULT_DB)
    CELERY_LOGLEVEL = _load_config("celery", "loglevel", CELERY_LOGLEVEL)
    CELERY_CONCURRENCY = _load_config("celery", "concurrency", CELERY_CONCURRENCY)
    CELERY_POOL = _load_config("celery", "pool", CELERY_POOL)
    CELERY_BEAT = _load_config("celery", "beat", CELERY_BEAT)
    CELERY_SCAN_INTERVAL = _load_config("celery", "scan_interval", CELERY_SCAN_INTERVAL)
    CELERY_BEAT_SCHEDULE_FILE = _load_config("celery", "beat_schedule_file", CELERY_BEAT_SCHEDULE_FILE)

    EMBEDDING_URL = _load_config("qwen-embedding", "url", EMBEDDING_URL)
    EMBEDDING_MODEL = _load_config("qwen-embedding", "model", EMBEDDING_MODEL)
    EMBEDDING_MODEL_KEY = _load_config("qwen-embedding", "key", EMBEDDING_MODEL_KEY)

    BGE_LARGE_ZH_MODEL_URL = _load_config("bge-large-zh", "url", BGE_LARGE_ZH_MODEL_URL)
    BGE_LARGE_ZH_MODEL_NAME = _load_config("bge-large-zh", "name", BGE_LARGE_ZH_MODEL_NAME)
    BGE_LARGE_ZH_MODEL_KEY = _load_config("bge-large-zh", "key", BGE_LARGE_ZH_MODEL_KEY)

    QWEN_RERANK_URL = _load_config("qwen-rerank", "url", QWEN_RERANK_URL)
    QWEN_RERANK_MODEL = _load_config("qwen-rerank", "model", QWEN_RERANK_MODEL)
    # 未单独配置（或配置为空）时复用硅基流动的 embedding key（与 bge-large-zh 同一控制台）
    QWEN_RERANK_MODEL_KEY = _load_config("qwen-rerank", "key", QWEN_RERANK_MODEL_KEY) or BGE_LARGE_ZH_MODEL_KEY
    QWEN_RERANK_INSTRUCT_FIELD = _load_config("qwen-rerank", "instruct_field", QWEN_RERANK_INSTRUCT_FIELD)

    BGE_RERANKER_V2_M3_MODEL_URL = _load_config("bge-reranker-v2-m3", "url", BGE_RERANKER_V2_M3_MODEL_URL)
    BGE_RERANKER_V2_M3_MODEL_NAME = _load_config("bge-reranker-v2-m3", "name", BGE_RERANKER_V2_M3_MODEL_NAME)
    BGE_RERANKER_V2_M3_MODEL_KEY = _load_config("bge-reranker-v2-m3", "key", BGE_RERANKER_V2_M3_MODEL_KEY)
