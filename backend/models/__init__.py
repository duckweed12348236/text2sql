from typing import Any, Sequence, Literal

from loguru import logger
from tortoise import Tortoise
from tortoise.models import Model as TortoiseModel
from tortoise.fields import ManyToManyRelation


class Model(TortoiseModel):
    excluded_fields = {"_partial", "_saved_in_db", "_custom_generated_pk", "_await_when_save"}
    connection = None

    def serialize(self) -> dict[str, Any]:
        pairs = {}
        excluded_fields = self.excluded_fields
        for key, value in self.__dict__.items():
            if key in excluded_fields:
                continue
            if key.startswith("_"):
                key = key[1:]

            if isinstance(value, Model):
                pairs[key] = value.serialize()
            elif ((isinstance(value, list) and len(value) > 0 and isinstance(value[0], Model)) or
                  isinstance(value, ManyToManyRelation)):
                pairs[key] = [x.serialize() for x in value]
            else:
                pairs[key] = value
        return pairs

    @classmethod
    async def init(cls):
        from config import TORTOISE_ORM_CONFIG
        await Tortoise.init(TORTOISE_ORM_CONFIG)
        cls.connection = Tortoise.get_connection("meta")

    @classmethod
    async def close(cls):
        cls.connection = None
        await Tortoise.close_connections()


async def get_column_values(table_name: str,
                            column_names: Sequence[str],
                            limit: int | None = None,
                            offset: int | None = None) -> dict[str, list[Any]]:
    if not column_names:
        return {}
    sql = f"select distinct {", ".join(column_names)} from {table_name}"
    if limit is not None and offset is not None:
        sql = f"{sql} limit {limit} offset {offset}"
    sql = f"{sql};"
    rows = await Model.connection.execute_query_dict(sql)
    values = {}
    for row in rows:
        for key, value in row.items():
            values.setdefault(key, []).append(value)
    return values


async def get_db_info() -> dict[str, Any]:
    connection = Model.connection
    dialect = connection.capabilities.dialect
    rows = await connection.execute_query_dict("select version() as version, @@character_set_database as charset;")
    return {"dialect": dialect, "version": rows[0]["version"], "charset": rows[0]["charset"]}


async def explain_sql(sql: str) -> tuple[bool, str | None]:
    try:
        await Model.connection.execute_query_dict(f"explain {sql};")
        return True, None
    except Exception as e:
        logger.error(e)
        return False, str(e)


async def execute_raw_sql(sql: str) -> dict[str, Any]:
    return await Model.connection.execute_query_dict(sql)
