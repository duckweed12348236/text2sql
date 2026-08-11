import asyncio
from typing import Optional
from elasticsearch import AsyncElasticsearch
from conf.app_config import ESConfig, app_config


class ESClient:
    def __init__(self, es_config: ESConfig):
        self.es_config = es_config
        self.es_url = f"http://{es_config.host}:{es_config.port}"
        self.client = AsyncElasticsearch(self.es_url)

    async def close(self):
        await self.client.close()

es_client = ESClient(app_config.es)

if __name__ == "__main__":
    client = es_client.client
    async def test():
        try:
            await client.indices.create(
                index="my-book",
                mappings={
                    "properties": {
                        "name": {
                            "type": "text"
                        },
                        "author": {
                            "type": "text"
                        },
                        "release_date": {
                            "type": "date",
                            "format": "yyyy-MM-dd"
                        },
                        "page_count": {
                            "type": "integer"
                        }
                    }
                }
            )
        finally:
            await client.close()
    
    asyncio.run(test())
