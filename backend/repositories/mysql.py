from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import delete, select
from models.meta import ColumnInfoMySQL, MetricInfoMySQL, TableInfoMySQL, ColumnMetricMySQL
from dtos.meta import ColumnInfo, TableInfo, MetricInfo, ColumnMetric
from sqlalchemy import text, and_
from typing import Any


class MetaDBRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def clear_all(self):
        await self.session.execute(delete(ColumnMetricMySQL))
        await self.session.execute(delete(MetricInfoMySQL))
        await self.session.execute(delete(ColumnInfoMySQL))
        await self.session.execute(delete(TableInfoMySQL))

    async def add_column_infos(self, column_infos: list[ColumnInfo]):
        self.session.add_all([ColumnInfoMySQL(**column_info.model_dump()) for column_info in column_infos])

    async def add_metric_infos(self, metric_infos: list[MetricInfo]):
        self.session.add_all([MetricInfoMySQL(**metric_info.model_dump()) for metric_info in metric_infos])

    async def add_column_metrics(self, column_metrics: list[ColumnMetric]):
        self.session.add_all([ColumnMetricMySQL(**column_metric.model_dump()) for column_metric in column_metrics])
    
    async def add_table_infos(self, table_infos: list[TableInfo]):
        self.session.add_all([TableInfoMySQL(**table_info.model_dump()) for table_info in table_infos])

    async def get_column_info_by_id(self, column_id: str) -> ColumnInfo | None:
        stmt = select(ColumnInfoMySQL).where(ColumnInfoMySQL.id == column_id)
        column_info_mysql = await self.session.scalar(stmt)
        if column_info_mysql:
            return ColumnInfo.model_validate(column_info_mysql)
        return None
    
    async def get_table_pfks_by_id(self, table_id: str) -> list[ColumnInfo]:
        stmt = select(ColumnInfoMySQL).where(
            and_(
                ColumnInfoMySQL.table_id == table_id,
                ColumnInfoMySQL.role.in_(['primary_key', 'foreign_key'])
            )
        )
        column_info_mysqls = await self.session.scalars(stmt)
        return [ColumnInfo.model_validate(column_info_mysql) for column_info_mysql in column_info_mysqls]
    
    async def get_table_info_by_id(self, table_id: str) -> TableInfo | None:
        stmt = select(TableInfoMySQL).where(TableInfoMySQL.id == table_id)
        table_info_mysql = await self.session.scalar(stmt)
        if table_info_mysql:
            return TableInfo.model_validate(table_info_mysql)
        return None


class DWDBRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_column_types(self, table_name: str) -> dict[str, str]:
        sql = f"show columns from {table_name};"
        result = await self.session.execute(text(sql))
        rows = result.fetchall()
        column_types: dict[str, str] = {row[0]: row[1] for row in rows}
        return column_types
    
    async def get_column_values(
        self, 
        table_name: str, 
        column_names: list[str],
        limit: int = 10,
        offset: int = 0
    ) -> dict[str, list[Any]]:
        sql = f"select distinct {",".join(column_names)} from {table_name} limit {limit} offset {offset};"
        result = await self.session.execute(text(sql))
        rows = result.fetchall()
        # {"region": ['a', 'b', 'c'], "customer_name": ['zhangsan', 'lisi']}
        column_values: dict[str, list[Any]] = {}
        for index, column_name in enumerate(column_names):
            # 这里没有做去重，可能出现比如：['男', '女', '女', '男']的情况
            column_values[column_name] = [row[index] for row in rows]
        
        return column_values
    
    async def get_db_info(self):
        version = await self.session.scalar(text("select version()"))
        # dialect+driver
        dialect = self.session.get_bind().dialect.name
        charset = await self.session.scalar(
            text(
                "SELECT DEFAULT_CHARACTER_SET_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = DATABASE()"
            )
        )
        return {"version": version, "dialect": dialect, "charset": charset}
    
    async def validate_sql(self, sql: str):
        await self.session.execute(text(f"explain {sql}"))

    async def execute_sql(self, sql: str):
        result = await self.session.execute(text(sql))
        rows = result.mappings().fetchall()
        return [dict(row) for row in rows]


    
if __name__ == "__main__":
    from clients.mysql import dw_mysql_client
    import asyncio
    async def test():
        async with dw_mysql_client.session() as session:
            dw_repo = DWDBRepository(session)
            column_values = await dw_repo.get_column_values("table_customer", ['customer_name', "gender"])
            print(column_values)
    asyncio.run(test())