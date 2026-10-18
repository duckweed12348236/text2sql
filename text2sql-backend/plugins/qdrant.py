from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from langchain_core.embeddings import Embeddings
from loguru import logger
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import VectorParams, Distance, QueryRequest, Filter, FilterSelector, FieldCondition, \
    MatchText, MatchAny, MatchValue, PointStruct, PayloadSchemaType

from models import VALUE_LIMIT, get_column_values, is_value_type
from models.meta import Column, Database, Metric
from plugins import Plugin
from config import QDRANT_URL

# 单次写入的字段数：1024 维向量约占 4KB/条，过大（如上万条）会让单次 upsert 请求体积失控
INSERT_BATCH_SIZE = 200


@dataclass()
class Value:
    payload: Any
    column_id: int
    type: str


# 取值入库时统一转成字符串（全文索引只收录字符串），原类型记在 payload["type"] 里，出库时按此还原。
# 键取类型自己的 __name__，与写库时记录的 type(value).__name__ 对齐；未登记的类型保持字符串原样。
# 采集范围只含整数与字符串类型（见 models.VALUE_TYPES）：字符串无需还原，故只需登记 int
# （整批都是整数列时取值为 int，混入字符串列会被 UNION 聚合为字符串）。新增采集类型时在这里补上。
RESTORERS = {int.__name__: int}

# 参与向量化的字段：字段名、描述、别名各成一个点（见 add_columns），它们一变，点的集合与向量都要重算
VECTORIZED_FIELDS = {"name", "description", "alias"}


def _restore(payload: dict[str, Any]) -> Value:
    """按入库时记录的类型，把取值的字符串形式还原成原类型。"""
    restorer = RESTORERS.get(payload["type"])
    return Value(payload=restorer(payload["payload"]) if restorer else payload["payload"],
                 column_id=payload["column_id"],
                 type=payload["type"])


def build_column_payload(column: Column) -> dict[str, Any]:
    """字段点的载荷：与元数据库的字段结构一致，便于 find_columns 直接还原成 Column（见 add_columns）。

    写入（add_columns）与更新载荷（update_column）共用这一份构造，两处写出的载荷才会完全一致。
    """
    return {"id": column.id,
            "name": column.name,
            "type": column.type,
            "role": column.role.value if column.role else None,
            "description": column.description,
            "alias": column.alias or [],
            "table_id": column.table_id,
            "synchronized": column.synchronized}


def build_columns_collection(database_id: int) -> str:
    """字段集合的集合名：字段按业务库分开存，集合名以数据库编号为后缀（集合见 register）。"""
    return f"columns.{database_id}"


def build_values_collection(database_id: int) -> str:
    """取值集合的集合名：取值按业务库分开存，集合名以数据库编号为后缀（集合见 register）。"""
    return f"values.{database_id}"


class Qdrant(Plugin):
    """Qdrant 向量库的读写封装。

    文本向量化不由本类负责：写入与检索都要求调用方把嵌入模型传进来
    （add_columns / find_columns / find_metrics 的 embedding_model 参数），
    保证写库与检索用的是同一个模型。
    """

    def __init__(self):
        self.client = AsyncQdrantClient(url=QDRANT_URL)

    async def find_columns(self, database_id: int, keywords: list[str], embedding_model: Embeddings) -> list[Column]:
        """按关键词检索字段：只在 database_id 指定的业务库的字段集合里检索（见 register），不跨库。"""
        embeddings = await embedding_model.aembed_documents(keywords)
        # 每个关键词在同一个（匿名）向量空间里各查一次（见 add_columns），命中结果按字段编号去重合并：
        # 同一字段的多个点（字段名、字段描述、若干别名）在这里合并成一条
        requests = [QueryRequest(query=embedding, with_payload=True) for embedding in embeddings]
        responses = await self.client.query_batch_points(build_columns_collection(database_id), requests)
        results = {}
        for res in responses:
            for point in res.points:
                if point.payload:
                    results.setdefault(point.payload["id"], Column(**point.payload))
        return list(results.values())

    async def find_metrics(self, keywords: list[str], embedding_model: Embeddings) -> list[Metric]:
        embeddings_list = await embedding_model.aembed_documents(keywords)
        requests = [QueryRequest(query=x,
                                 with_payload=True) for x in embeddings_list]
        responses = await self.client.query_batch_points("metrics", requests)
        results = {}
        for res in responses:
            for point in res.points:
                if point.payload:
                    results.setdefault(point.payload["id"], Metric(**point.payload))
        return list(results.values())

    async def find_values(self, database_id: int, keywords: list[str]) -> list[Value]:
        """按关键词检索字段取值：只在 database_id 指定的业务库的取值集合里检索（见 register），不跨库。"""
        conditions = [
            FieldCondition(key="payload", match=MatchText(text=keyword))
            for keyword in keywords if keyword
        ]
        if not conditions:
            return []
        records, _ = await self.client.scroll(
            collection_name=build_values_collection(database_id),
            scroll_filter=Filter(should=conditions),
            with_payload=True
        )
        results = {}
        for record in records:
            if record.payload:
                results.setdefault(record.id, _restore(record.payload))
        return list(results.values())

    async def add_columns(self, database_id: int, columns: list[Column], embedding_model: Embeddings) -> None:
        """向向量数据库批量插入字段信息。

        - database_id 指定这批字段所属的业务库，决定写进哪一个字段集合（columns.<数据库编号>，
          见 register）：字段与取值都按库分开存，检索时不会串库；
        - 字段名、字段描述、字段别名一起提交给嵌入模型向量化，每个文本各成一个点：字段名一个点、
          字段描述一个点、每个别名各一个点；三种文本写进同一个（匿名）向量空间，故 find_columns 每个
          关键词只查一次；空的描述、没有别名的字段就不写对应的点；
          嵌入模型由调用方传入（图上下文的 Context.embedding_model），保证写库与检索同源；
        - 一个字段的多个别名各自成点（不拼成一段文本再向量化）：多个别名合成一个向量会让彼此的语义
          互相稀释，任一个别名的表达都难以独立命中；
        - payload 与元数据库的字段结构一致，便于 find_columns 直接还原成 Column；同一字段的若干个点
          共用同一份 payload，find_columns 按字段编号去重后，结果与「一个字段一个点」时一致；
        - 点编号取 uuid4（Qdrant 要求点必须自带编号，服务端不会代生成）：编号不承载「字段 + 文本」的
          语义；代价是 upsert 不再是「同编号覆盖」，重复采集会不断堆积重复点，故写入前先按 payload 的
          字段编号删掉这批字段的全部旧点（顺带清掉旧编号策略留下的点）。
        """
        collection = build_columns_collection(database_id)
        for start in range(0, len(columns), INSERT_BATCH_SIZE):
            chunk = columns[start:start + INSERT_BATCH_SIZE]
            payloads = [build_column_payload(column) for column in chunk]
            # 字段名、字段描述、每个别名放进同一次提交（文本与向量的位置一一对应，见 find_columns）；
            # 文本按所属字段的下标配对，落库时据此挂上该字段的 payload
            texts = [(index, text) for index, column in enumerate(chunk)
                     for text in (column.name, column.description, *(column.alias or [])) if text]
            embeddings = await embedding_model.aembed_documents([text for _, text in texts])
            points = [PointStruct(id=str(uuid4()), vector=embedding, payload=payloads[index])
                      for (index, _), embedding in zip(texts, embeddings)]
            await self.client.upsert(collection, points)
            logger.info(f"add_columns: {min(start + INSERT_BATCH_SIZE, len(columns))}/{len(columns)}")

    async def add_values(self, database_id: int, values: list[Value]) -> None:
        """向向量数据库批量插入字段取值。

        - database_id 指定这批取值所属的业务库，决定写进哪一个取值集合（values.<数据库编号>，
          见 register）：取值与字段一样按库分开存；
        - 取值集合只做 payload 检索（find_values 用 MatchText 过滤 payload），因此不写向量；
        - 取值统一转成字符串后写入（全文索引只收录字符串），原类型记在 payload["type"] 里，
          由 find_values 出库时按类型还原；
        - 点编号取 uuid4（与 add_columns 一致，Qdrant 要求点必须自带编号、服务端不会代生成）：编号不承载
          「字段 + 取值」的语义；代价是 upsert 不再是「同编号覆盖」，重复采集会不断堆积重复点，故写入前
          先按 payload 的字段编号删掉这批字段的旧取值点（依赖 column_id 上的关键字索引，见 register）。
        """
        collection = build_values_collection(database_id)
        for start in range(0, len(values), INSERT_BATCH_SIZE):
            chunk = values[start:start + INSERT_BATCH_SIZE]
            points = [
                PointStruct(id=str(uuid4()),
                            vector={},
                            payload={"payload": str(x.payload),
                                     "column_id": x.column_id,
                                     "type": x.type})
                for x in chunk
            ]
            await self.client.upsert(collection, points)
            logger.info(f"add_values: {min(start + INSERT_BATCH_SIZE, len(values))}/{len(values)}")

    async def delete_database(self, database_id: int) -> None:
        """删掉一个业务库在向量库里的全部数据：字段集合（columns.<数据库编号>）与取值集合
        （values.<数据库编号>）各是一个集合，整集合删除。

        删除业务库时调用（见 controller 的 delete_database）：库都删了，挂在它名下的字段点与取值点
        也不会再被检索，留着的只是占空间的孤儿集合。集合不存在（本就没采集过，或已删过）时静默跳过，
        故重复调用是安全的。指标集合（metrics）跨库共用，不在删除范围内。
        """
        for collection in (build_columns_collection(database_id), build_values_collection(database_id)):
            if not await self.client.collection_exists(collection):
                continue
            await self.client.delete_collection(collection)
            logger.info(f"delete_database: collection {collection} dropped")

    async def delete_values(self, database_id: int, column_ids: list[str]) -> None:
        """按字段编号删除取值点：删掉这些字段在向量库里登记的全部取值（不再是可用取值时调用）。

        - database_id 指定这些字段所属的业务库，决定删哪个取值集合（values.<数据库编号>，见 register）：
          取值按库分开存，不跨库删；
        - 过滤条件取 payload 的 column_id，与 add_values 写入时挂的字段一致，走 register 建好的关键字索引；
          编号用 MatchAny 一次带上，多个字段的取值一次请求删完，不必逐字段往返（字段编号可只传一个）；
        - 只删向量库里的点，不动元数据库：字段自身的登记与同步标记仍归调用方维护；
        - 空列表直接返回：Qdrant 不接受空的 MatchAny，且没有编号本来也不该命中任何点。
        """
        if not column_ids:
            return
        collection = build_values_collection(database_id)
        await self.client.delete(
            collection_name=collection,
            points_selector=FilterSelector(filter=Filter(
                must=[FieldCondition(key="column_id", match=MatchAny(any=column_ids))])
            )
        )
        logger.info(f"delete_values: removed all value(s) of {len(column_ids)} column(s) in {collection}")

    async def delete_columns(self, database_id: int, column_ids: list[str]) -> None:
        """按字段编号删除字段点：把字段名 / 描述 / 别名写下的点整批清掉。

        - database_id 指定这些字段所属的业务库，决定删哪个字段集合（columns.<数据库编号>，见 register）：
          字段按库分开存，不跨库删；
        - 过滤条件取 payload 的 id，与 add_columns 写入时挂的字段一致，走 register 建好的关键字索引；
          编号用 MatchAny 一次带上，多个字段的点一次请求删完，不必逐字段往返；
        - 两个调用方：重新向量化前先清旧点（见 update_column）；表已从业务库里移除时清掉其字段的点
          （见 plugins.celery.tasks 的巡检——字段不会再被采集，留下的点只会被 find_columns 召回，
          拼出的 SQL 必然执行失败）。取值点不在删除范围内（见 delete_values），清表时两者一起删；
        - 只删向量库里的点，不动元数据库：字段自身的登记仍归调用方维护；
        - 空列表直接返回：Qdrant 不接受空的 MatchAny。
        """
        if not column_ids:
            return
        await self.client.delete(
            collection_name=build_columns_collection(database_id),
            points_selector=FilterSelector(filter=Filter(
                must=[FieldCondition(key="id", match=MatchAny(any=column_ids))])
            )
        )
        logger.info(f"delete_columns: removed all point(s) of {len(column_ids)} column(s)")

    async def update_column(self,
                            database_id: int,
                            column: Column,
                            updated: set[str],
                            embedding_model: Embeddings) -> None:
        """把字段的改动同步到向量库：更新字段载荷，必要时重新向量化，并维护取值点。

        :param database_id: 该字段所属的业务库编号，决定写哪一套集合（columns.<数据库编号> /
            values.<数据库编号>，见 register）与取哪个业务库连接（database.<数据库编号>）
        :param column: 改动后的字段（元数据库里的最新状态）
        :param updated: 本次显式改动的字段名集合（调用方给，如 controller 里的 updates.keys()）
        :param embedding_model: 嵌入模型，与检索侧同源（见类说明），仅在需要重新向量化时用到

        - 字段名 / 描述 / 别名参与向量化（各成一个点，见 add_columns）：它们变了就意味着点的集合变了
          （别名数量可增减、文本也变了），旧点整批作废，先按字段编号删点再整批重写；别名的数量、
          顺序都按 column 上的现值为准；
        - 其余字段（类型、角色、同步标记等）按 column 覆盖到现有点的载荷上，不动向量：只改这些字段时
          不必重新调用嵌入模型；
        - synchronized 由 False 变 True：按取值采集口径重新取回该字段的取值写进取值集合；由 True 变
          False：删掉该字段的全部取值点（不再是可用取值）。字段类型不属于 VALUE_TYPES 时不碰取值集合，
          与采集图的取值范围一致（见 models.is_value_type）；
        - 只动向量库，不动元数据库：字段自身的登记仍归调用方维护（见 controller 的 update_column）。
        """
        if VECTORIZED_FIELDS & updated:
            # 先删后写：点编号随机，不先清旧点会与本次写入的点并存（旧别名的向量会一直留在库里）
            await self.delete_columns(database_id, [column.id])
            await self.add_columns(database_id, [column], embedding_model)
        else:
            await self.client.set_payload(
                collection_name=build_columns_collection(database_id),
                payload=build_column_payload(column),
                points=FilterSelector(filter=Filter(
                    must=[FieldCondition(key="id", match=MatchValue(value=column.id))])
                )
            )
            logger.info(f"update_column: payload updated for {column.id}")

        if "synchronized" not in updated:
            return
        if not column.synchronized:
            # 关掉同步：取值不再作为可用取值，该字段的取值点整批删掉
            await self.delete_values(database_id, [column.id])
            return
        if not is_value_type(column.type):
            logger.info(f"update_column: {column.id} is not a value type, skip value collection")
            return
        # 表编号形如 <库编号>.<表名>，取值拼 SQL 用的是业务库表名（与 discover_tables 取出表名的口径一致）
        table_name = column.table_id.removeprefix(f"{database_id}.")
        # 先取回取值再清旧点：取数失败时不至于把已有的取值点删空
        values = await get_column_values([(table_name, column.name, VALUE_LIMIT, 0)], database_id)
        points = [Value(payload=value, column_id=column.id, type=type(value).__name__)
                  for value in values.get(f"{table_name}.{column.name}", [])
                  # NULL 不是可用取值，全文索引也不会收录，直接丢弃（与采集图一致）
                  if value is not None]
        await self.delete_values(database_id, [column.id])
        await self.add_values(database_id, points)
        logger.info(f"update_column: collected {len(points)} value(s) for {column.id}")

    async def register(self) -> None:
        # metrics 不归属于具体业务库（指标跨库使用），全库共用一份：按整体文本一个向量，用默认的匿名向量
        if not await self.client.collection_exists("metrics"):
            await self.client.create_collection("metrics", VectorParams(size=1024, distance=Distance.COSINE))
        # 字段集合与取值集合按业务库分开建：遍历元数据库里已登记的库，每个库各建一套（集合名见 add_columns /
        # add_values 用的 columns_collection / values_collection）。集合与索引都在这里建好，写入与检索就不必
        # 每次先判断集合是否存在；元数据库里删掉的库不回收集合，残留集合不影响其它库
        for database_id in await Database.all().values_list("id", flat=True):
            await self._register_database(database_id)

    async def _register_database(self, database_id: int) -> None:
        """为单个业务库建字段集合与取值集合，并建好写入 / 检索所依赖的 payload 索引。"""
        # columns 按整体文本一个向量（匿名）：字段名 / 字段描述 / 每个别名各成一个点，与 add_columns 口径一致
        columns = build_columns_collection(database_id)
        if not await self.client.collection_exists(columns):
            await self.client.create_collection(columns, VectorParams(size=1024, distance=Distance.COSINE))
        # add_columns 写入前按 payload 的字段编号删除这批字段的旧点（点编号随机、别名数量会增减），
        # 该过滤条件依赖 id 上的关键字索引，否则每次写入都要全表扫
        await self.client.create_payload_index(columns, field_name="id", field_schema=PayloadSchemaType.KEYWORD)
        # values 只做 payload 检索（find_values 用 MatchText 过滤 payload），不需要向量，故以空向量配置建集合
        # （add_values 以空向量 {} 写入）。缺这个集合时 upsert 会报集合不存在、find_values 也无从检索
        values = build_values_collection(database_id)
        if not await self.client.collection_exists(values):
            await self.client.create_collection(values, vectors_config={})
        # values 的 payload 字段需要全文索引：find_values 用 MatchText 过滤 payload，没有全文索引 Qdrant 会
        # 直接拒绝该过滤条件。create_payload_index 可重复执行，已存在时按新配置覆盖。
        await self.client.create_payload_index(values, field_name="payload", field_schema=PayloadSchemaType.TEXT)
        # add_values 写入前按 payload 的字段编号删除这批字段的旧取值点（同 add_columns 的覆盖语义），
        # 该过滤条件依赖 column_id 上的关键字索引，否则每次写入都要全表扫
        await self.client.create_payload_index(values, field_name="column_id",
                                               field_schema=PayloadSchemaType.KEYWORD)

    async def close(self) -> None:
        await self.client.close()
