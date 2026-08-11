from elasticsearch import AsyncElasticsearch
from dtos.es import ValueInfo
from typing import Any


class ESRepository:
    index_name: str = "value_info"
    index_mappings: dict[str, dict[str, Any]] = {
        "properties": {
            # keyword类型：常用语结构化数据，比如ID、手机号、邮箱、性别，keyword类型的不会被分词
            "id": {"type": "keyword"},
            # IK提供了两种分词器，分别是
            # ik_smart：最少切分
            # ik_max_word：最细粒度划分
            "value": {"type": "text", "analyzer": "ik_max_word", "search_analyzer": "ik_max_word"},
            "column_id": {"type": "keyword"}
        }
    }

    def __init__(self, client: AsyncElasticsearch):
        self.client = client

    async def clear_all(self):
        if (await self.client.indices.exists(index=self.index_name)):
            await self.client.indices.delete(index=self.index_name)

    async def ensure_index(self):
        if not (await self.client.indices.exists(index=self.index_name)):
            await self.client.indices.create(index=self.index_name, mappings=self.index_mappings)

    async def add_documents(self, value_infos: list[ValueInfo]):
        batch_size: int = 20
        for index in range(0, len(value_infos), batch_size):
            batch_infos = value_infos[index: index+batch_size]
            operations = []
            for info in batch_infos:
                operations.append({"index": {"_index": self.index_name, "_id": info.id}})
                operations.append(info.model_dump())
            # 批量插入数据到es中
            await self.client.bulk(operations=operations)

    async def search(
        self,
        keyword: str,
        score_threshold: float = 0.6,
        limit: int = 5
    ) -> list[ValueInfo]:
        result = await self.client.search(
            index=self.index_name,
            query={
                "match": {
                    "value": keyword
                }
            },
            min_score=score_threshold,
            size=limit
        )
        return [ValueInfo.model_validate(hit['_source']) for hit in result['hits']['hits']]