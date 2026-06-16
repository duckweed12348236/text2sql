from typing import TypedDict

from elasticsearch import AsyncElasticsearch

from models import get_column_values
from models.meta import Column, Table
from plugins import Plugin
from plugins.snowflake import Snowflake
from config import ELASTICSEARCH_URL

Shape = dict[str, dict[str, str]]


class Document(TypedDict):
    id: int
    payload: str
    column_id: str


class ES(Plugin):
    snowflake = Snowflake()

    def __init__(self):
        self.client = AsyncElasticsearch(ELASTICSEARCH_URL)
        self.batch_size = 1000

    async def create_index(self, name: str, shape: Shape) -> None:
        result = await self.client.indices.exists(index=name)
        if not result:
            await self.client.indices.create(index=name, mappings={"properties": shape})

    async def remove_index(self, name: str) -> None:
        result = await self.client.indices.exists(index=name)
        if result:
            await self.client.indices.delete(index=name)

    async def add_documents(self, index_name: str, documents: list[Document]):
        for i in range(0, len(documents), self.batch_size):
            batch = documents[i:i + self.batch_size]
            operations = []
            for doc in batch:
                operations.append({"index": {"_index": index_name, "_id": doc["id"]}})
                operations.append(doc)
            await self.client.bulk(
                operations=operations,
                refresh=True
            )

    async def search_documents(self,
                               keywords: list[str],
                               index_name: str,
                               score_threshold: float = 0.6,
                               limit=5) -> list[Document]:
        documents = []
        for keyword in keywords:
            documents.append({})
            documents.append({
                "size": limit,
                "min_score": score_threshold,
                "query": {
                    "match": {
                        "payload": keyword
                    }
                }
            })
        results = await self.client.msearch(index=index_name, searches=documents)
        values = []
        for res in results["responses"]:
            if "hits" in res:
                values.extend([x["_source"] for x in res["hits"]["hits"]])
        return values

    async def init(self) -> None:
        await self.remove_index("values")
        await self.create_index("values", {
            "id": {"type": "keyword"},
            "payload": {"type": "text", "analyzer": "ik_max_word", "search_analyzer": "ik_max_word"},
            "column_id": {"type": "keyword"}
        })
        documents = []
        tables = await Table.all()
        for table in tables:
            columns = await table.columns.filter(synchronized=True).all()
            column_values = await get_column_values(table.name, [x.name for x in columns])
            for column in columns:
                documents.extend([{
                    "id": self.snowflake.get_id(),
                    "payload": x,
                    "column_id": column.id
                } for x in list(set(column_values[column.name]))])
        await self.add_documents("values", documents)

    async def close(self) -> None:
        await self.client.close()
