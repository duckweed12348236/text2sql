from langchain_community.embeddings import DashScopeEmbeddings

from plugins import Singleton
from config import EMBEDDING_MODEL, EMBEDDING_MODEL_KEY


class EmbeddingClient(Singleton):
    def __init__(self):
        self.client = DashScopeEmbeddings(model=EMBEDDING_MODEL, dashscope_api_key=EMBEDDING_MODEL_KEY)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return await self.client.aembed_documents(texts)
