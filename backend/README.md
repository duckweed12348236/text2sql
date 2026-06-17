# Text2SQL Backend

## 环境要求

- Python >= 3.14
- MySQL（默认 `root:123456@localhost:3306/text2sql`）
- Qdrant（向量数据库，默认 `http://localhost:6333`）
- Elasticsearch（默认 `http://localhost:9200`）

## 快速开始

### 1. 安装依赖

推荐使用 [uv](https://docs.astral.sh/uv/) 管理依赖：

```bash
uv sync
```

或使用 pip：

```bash
pip install -e .
```

### 2. 配置 API Key

编辑 `config.py` 文件，填写以下两个 API Key：

```python
# DeepSeek 大模型 API Key
# 获取地址: https://platform.deepseek.com/api_keys
DEEPSEEK_MODEL_KEY = "你的DeepSeek-API-Key"

# Embedding 模型 API Key（阿里云百炼）
# 获取地址: https://dashscope.console.aliyun.com/apiKey
EMBEDDING_MODEL_KEY = "你的百炼-API-Key"
```

| 配置项 | 说明 | 获取地址 |
|--------|------|----------|
| `DEEPSEEK_MODEL_KEY` | DeepSeek 大语言模型密钥，用于生成 SQL | [DeepSeek API Keys](https://platform.deepseek.com/api_keys) |
| `EMBEDDING_MODEL_KEY` | 阿里云百炼 Embedding 密钥，用于文本向量化 | [百炼 API Key](https://dashscope.console.aliyun.com/apiKey) |

其他可配置项（按需修改）：

- `SERVER_HOST` / `SERVER_PORT` — 服务地址和端口
- `MYSQL_URL` — MySQL 数据库连接地址
- `QDRANT_URL` — Qdrant 向量数据库地址
- `ELASTICSEARCH_URL` — Elasticsearch 地址

### 3. 确保依赖服务已启动

启动前请确保 MySQL、Qdrant、Elasticsearch 服务均已运行。

### 4. 启动服务

```bash
python app.py
```

服务将在 `http://127.0.0.1:8000` 启动。

API 文档可访问：

- Swagger UI: http://127.0.0.1:8000/docs
- ReDoc: http://127.0.0.1:8000/redoc
