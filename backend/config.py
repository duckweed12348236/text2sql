from pkgutil import iter_modules

SERVER_HOST = "127.0.0.1"
SERVER_PORT = 8000
SERVER_URL = f"http://{SERVER_HOST}:{SERVER_PORT}"

MYSQL_URL = "mysql://root:123456@localhost:3306/text2sql"

CORS_ALLOW_ORIGINS = ["*"]
CORS_ALLOW_CREDENTIALS = True
CORS_ALLOW_METHODS = ["*"]
CORS_ALLOW_HEADERS = ["*"]
CORS_EXPOSE_HEADERS = ["*"]

import models

TORTOISE_ORM_CONFIG = {
    "connections": {
        "meta": MYSQL_URL
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

DEEPSEEK_MODEL_KEY = ""
DEEPSEEK_MODEL_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL_NAME = "deepseek-chat"

QDRANT_URL = "http://localhost:6333"

ELASTICSEARCH_URL = "http://localhost:9200"

EMBEDDING_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
EMBEDDING_MODEL = "text-embedding-v4"
EMBEDDING_MODEL_KEY = ""
