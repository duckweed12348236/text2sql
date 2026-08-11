from pydantic import BaseModel, ConfigDict
from omegaconf import OmegaConf
from pathlib import Path

class FileLoggingConfig(BaseModel):
    enable: bool
    level: str
    path: str
    rotation: str
    retention: str
    model_config = ConfigDict(from_attributes=True)


class ConsoleLoggingConfig(BaseModel):
    enable: bool
    level: str
    model_config = ConfigDict(from_attributes=True)


class LoggingConfig(BaseModel):
    file: FileLoggingConfig
    console: ConsoleLoggingConfig
    model_config = ConfigDict(from_attributes=True)


class DBConfig(BaseModel):
    host: str
    port: int
    user: str
    password: str
    database: str
    model_config = ConfigDict(from_attributes=True)


class QDrantConfig(BaseModel):
    host: str
    port: int
    embedding_size: int
    model_config = ConfigDict(from_attributes=True)


class EmbeddingConfig(BaseModel):
    host: str
    port: int
    enable: bool
    model_config = ConfigDict(from_attributes=True)


class EmbeddingFallbackConfig(BaseModel):
    base_url: str
    api_key: str
    model_name: str
    model_config = ConfigDict(from_attributes=True)


class ESConfig(BaseModel):
    host: str
    port: int
    index_name: str
    model_config = ConfigDict(from_attributes=True)


class LLMConfig(BaseModel):
    model_name: str
    api_key: str
    base_url: str
    model_config = ConfigDict(from_attributes=True)


class AppConfig(BaseModel):
    logging: LoggingConfig
    db_meta: DBConfig
    db_dw: DBConfig
    qdrant: QDrantConfig
    es: ESConfig
    embedding: EmbeddingConfig
    embedding_fallback: EmbeddingFallbackConfig
    llm: LLMConfig


config_path = Path(__file__).parent / "app_config.yaml"
context = OmegaConf.load(config_path)
app_config: AppConfig = AppConfig.model_validate(context)