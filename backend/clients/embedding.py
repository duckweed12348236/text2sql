from langchain_openai import OpenAIEmbeddings
from conf.app_config import EmbeddingConfig, app_config
from pydantic import SecretStr


class EmbeddingClient:
    def __init__(self):
        embedding_config = app_config.embedding
        if embedding_config.enable:
            embedding_url = f"http://{embedding_config.host}:{embedding_config.port}"
            self.client = OpenAIEmbeddings(
                base_url=embedding_url,
                api_key=SecretStr("..."),
                model="..."
            )
        else:
            embedding_fallback_config = app_config.embedding_fallback
            self.client = OpenAIEmbeddings(
                base_url=embedding_fallback_config.base_url,
                api_key=SecretStr(embedding_fallback_config.api_key),
                model=embedding_fallback_config.model_name,
                tiktoken_enabled=False,
                check_embedding_ctx_length=False
            )

embedding_client = EmbeddingClient()

if __name__ == "__main__":
    import asyncio
    async def test():
        vector = await embedding_client.client.aembed_query("你是谁？")
        print(vector[:10])
    asyncio.run(test())
    
