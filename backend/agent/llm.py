from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from conf.app_config import app_config

llm = ChatOpenAI(
    model=app_config.llm.model_name,
    api_key=SecretStr(app_config.llm.api_key),
    base_url=app_config.llm.base_url,
    temperature=0
)