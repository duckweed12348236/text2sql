import asyncio
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import VectorParams, Distance
from conf.app_config import QDrantConfig, app_config


class QDrantClient:
    def __init__(self, qdrant_config: QDrantConfig):
        self.qdrant_config = qdrant_config
        self.qdrant_url = f"http://{qdrant_config.host}:{qdrant_config.port}"
        self.client = AsyncQdrantClient(self.qdrant_url)
    
    async def ensure_collection(self, collection_name: str):
        exists = await self.client.collection_exists(collection_name=collection_name)
        if not exists:
            await self.client.create_collection(
                collection_name=collection_name,
                vectors_config=VectorParams(
                    size=self.qdrant_config.embedding_size,
                    distance=Distance.COSINE
                )
            )
        
    async def close(self):
        await self.client.close()

qdrant_client = QDrantClient(app_config.qdrant)


if __name__ == "__main__":
    async def test():
        await qdrant_client.ensure_collection("test_collection")

    asyncio.run(test())