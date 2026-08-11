from agent.schemas import WSAgentState, WSAgentContext, WSStepInfo, WSAgentTableInfoState
from langgraph.runtime import Runtime
from dtos.meta import ColumnInfo, ValueInfo, MetricInfo, TableInfo
from core.log import logger


async def merge_recalled_infos(state: WSAgentState, runtime: Runtime[WSAgentContext]):
    # 实时反馈节点执行的状态
    writer = runtime.stream_writer
    writer(WSStepInfo(step="合并召回信息", status="running"))

    recalled_columns: list[ColumnInfo] = state.recalled_columns
    recalled_metrics: list[MetricInfo] = state.recalled_metrics
    recalled_values: list[ValueInfo] = state.recalled_values

    meta_db_repo = runtime.context.meta_db_repo

    # 1. 合并表信息
    # 1.1. 将召回的字段和指标中关联的字段合并在一起
    columns_mapping: dict[str, ColumnInfo] = {
        column_info.id: column_info
        for column_info in recalled_columns
    }
    for recalled_metric in recalled_metrics:
        relevant_columns: list[str] = recalled_metric.relevant_columns
        for relevant_column_id in relevant_columns:
            if relevant_column_id not in columns_mapping:
                column_info = await meta_db_repo.get_column_info_by_id(relevant_column_id)
                if column_info is not None:
                    columns_mapping[relevant_column_id] = column_info
    
    # 1.2. 将召回的值添加到对应字段的examples中
    for recalled_value in recalled_values:
        column_id = recalled_value.column_id
        if column_id not in columns_mapping:
            column_info = await meta_db_repo.get_column_info_by_id(column_id)
            if column_info is not None:
                columns_mapping[column_id] = column_info
            else:
                continue
        column_info: ColumnInfo = columns_mapping[column_id]
        if recalled_value.value not in column_info.examples:
            column_info.examples.append(recalled_value.value)
        
    # 1.3. 将字段放到对应的表下面
    table_columns_mapping: dict[str, list[ColumnInfo]] = {}
    for column_info in columns_mapping.values():
        table_id = column_info.table_id
        if table_id not in table_columns_mapping:
            table_columns_mapping[table_id] = [column_info]
        else:
            table_columns_mapping[table_id].append(column_info)
    
    # 1.4. 补充每个表的主键和外键
    for table_id in table_columns_mapping.keys():
        # 根据table_id从数据库中获取该表的主键和外键
        column_infos = await meta_db_repo.get_table_pfks_by_id(table_id)
        table_column_ids = [
            column_info.id
            for column_info in table_columns_mapping[table_id]
        ]
        for column_info in column_infos:
            if column_info.id not in table_column_ids:
                table_columns_mapping[table_id].append(column_info)
    
    # 1.5. 将表和字段的字段转换为list[WSAgentTableInfoState]
    table_infos: list[WSAgentTableInfoState] = []
    for table_id, column_infos in table_columns_mapping.items():
        # 根据table_id从数据库中获取表信息
        table_info: TableInfo | None = await meta_db_repo.get_table_info_by_id(table_id)
        if table_info is not None:
            table_infos.append(
                WSAgentTableInfoState(
                    id=table_id,
                    name=table_info.name,
                    role=table_info.role,
                    description=table_info.description,
                    columns=column_infos
                )
            )
        
    # 2. 处理指标信息
    metric_infos: list[MetricInfo] = [metric_info for metric_info in recalled_metrics]

    writer(WSStepInfo(step="合并召回信息", status="success"))

    # logger.info([
    #     (table_info.name, [column_info.name for column_info in table_info.columns])
    #     for table_info in table_infos
    # ])
    # logger.info([metric.name for metric in metric_infos])

    return {"table_infos": table_infos, "metric_infos": metric_infos}
