from typing import Any, TypedDict

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import VectorParams, Distance, PointStruct, QueryRequest

from models.meta import Column, Metric
from plugins import Plugin
from config import QDRANT_URL
from plugins.embedding import EmbeddingClient
from plugins.snowflake import Snowflake


class Point(TypedDict):
    keywords: list[str]
    payload: dict[str, Any] | None


class Qdrant(Plugin):
    snowflake = Snowflake()
    embedding_client = EmbeddingClient()

    def __init__(self):
        self.client = AsyncQdrantClient(url=QDRANT_URL)
        self.batch_size = 500

    async def create_collection(self, name: str) -> None:
        result = await self.client.collection_exists(name)
        if not result:
            await self.client.create_collection(name, VectorParams(size=1024, distance=Distance.COSINE))

    async def remove_collection(self, name: str) -> None:
        await self.client.delete_collection(name)

    async def search_points(self,
                            index_name: str,
                            keywords: list[str],
                            score_threshold: float = 0.6,
                            limit: int = 10) -> list[dict[str, Any] | None]:
        embeddings_list = await self.embedding_client.embed(keywords)
        requests = [QueryRequest(query=x,
                                 score_threshold=score_threshold,
                                 limit=limit,
                                 with_payload=True) for x in embeddings_list]
        responses = await self.client.query_batch_points(index_name, requests)
        payloads = []
        for res in responses:
            payloads.extend([x.payload for x in res.points])
        return payloads

    async def save_points(self, index_name: str, points: list[Point]) -> None:
        point_structs = []
        for point in points:
            embeddings_list = await self.embedding_client.embed(point["keywords"])
            point_structs.extend([
                PointStruct(id=self.snowflake.get_id(),
                            vector=x,
                            payload=point["payload"])
                for x in embeddings_list
            ])
        for i in range(0, len(point_structs), self.batch_size):
            batch = point_structs[i:i + self.batch_size]
            await self.client.upsert(index_name, batch)

    async def sync_columns(self):
        columns = await Column.all()
        points = []
        for column in columns:
            points.append({
                "keywords": column.alias,
                "payload": column.serialize()
            })
        await self.save_points("columns", points)

    async def sync_metrics(self):
        metrics = await Metric.all()
        points = []
        for metric in metrics:
            points.append({
                "keywords": [metric.name, *metric.alias],
                "payload": metric.serialize()
            })
        await self.save_points("metrics", points)

    async def init(self) -> None:
        await self.remove_collection("columns")
        await self.remove_collection("metrics")
        await self.create_collection("columns")
        await self.create_collection("metrics")
        await self.sync_columns()
        await self.sync_metrics()

    async def close(self) -> None:
        await self.client.close()
