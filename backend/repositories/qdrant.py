from qdrant_client import AsyncQdrantClient
from qdrant_client.models import VectorParams, Distance, PointStruct, Filter
from typing import Sequence
from dtos.qdrant import ColumnQdrantInfo, MetricQdrantInfo, BaseQdrantInfo
from abc import ABC
from dtos.meta import ColumnInfo, MetricInfo


class BaseQdrantRepository(ABC):
    collection_name: str

    def __init__(self, client: AsyncQdrantClient):
        self.client: AsyncQdrantClient = client

    async def clear_all(self):
        if (await self.client.collection_exists(self.collection_name)):
            await self.client.delete_collection(
                self.collection_name
            )
    
    async def ensure_collection(self):
        if not (await self.client.collection_exists(self.collection_name)):
            await self.client.create_collection(
                self.collection_name,
                vectors_config=VectorParams(
                    size=1024,
                    distance=Distance.COSINE
                )
            )
        
    async def upsert(self, qdrant_infos: Sequence[BaseQdrantInfo]):
        batch_size: int = 20
        for index in range(0, len(qdrant_infos), batch_size):
            batch_infos: Sequence[BaseQdrantInfo] = qdrant_infos[index: index+batch_size]
            points = [
                PointStruct(id=info.id, vector=info.embeddings, payload=info.payload.model_dump())
                for info in batch_infos
            ]
            await self.client.upsert(self.collection_name, points=points)

    async def _search(
        self,
        embedding: list[float],
        score_threshold: float = 0.6,
        limit: int=5
    ) -> list:
        result = await self.client.query_points(
            collection_name=self.collection_name,
            query=embedding,
            score_threshold=score_threshold,
            limit=limit
        )
        return [point.payload for point in result.points]


class ColumnQdrantRepository(BaseQdrantRepository):
    collection_name: str = "column_info"

    async def upsert(self, qdrant_infos: list[ColumnQdrantInfo]):
        await super().upsert(qdrant_infos)

    async def search(
        self, 
        embedding: list[float], 
        score_threshold: float = 0.6, 
        limit: int = 5
    ) -> list[ColumnInfo]:
        payloads: list[dict] = await super()._search(embedding, score_threshold, limit)
        return [ColumnInfo.model_validate(payload) for payload in payloads]


class MetricQdrantRepository(BaseQdrantRepository):
    collection_name: str = "metric_info"

    async def upsert(self, qdrant_infos: list[MetricQdrantInfo]):
        await super().upsert(qdrant_infos)

    async def search(
        self, 
        embedding: list[float], 
        score_threshold: float = 0.6, 
        limit: int = 5
    ) -> list[MetricInfo]:
        payloads: list[dict] = await super()._search(embedding, score_threshold, limit)
        return [MetricInfo.model_validate(payload) for payload in payloads]
