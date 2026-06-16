from typing import Any, Callable
from datetime import datetime
from zoneinfo import ZoneInfo
from functools import update_wrapper

from langchain_core.messages import SystemMessage
from langgraph.runtime import Runtime
from langgraph.types import Command
from langgraph.graph import END
from jieba.analyse import extract_tags
from loguru import logger
from pydantic import ValidationError

from models import get_db_info, explain_sql, execute_raw_sql
from models.meta import Column, ColumnRoleOption, Table
from plugins.agent.models import deepseek_model as model
from schemas import str_list_adapter, str_key_any_value_dict_adapter, str_adapter
from plugins.agent.schemas import State, Step, StepStatus, Intention, ColumnInfo, MetricInfo, ValueInfo, TableInfo
from plugins.agent.prompts import INTENTION_ANALYSIS_PROMPT, COLUMN_KEYWORD_EXTENSION_PROMPT_TEMPLATE, \
    METRIC_KEYWORD_EXTENSION_PROMPT_TEMPLATE, VALUE_KEYWORD_EXTENSION_PROMPT_TEMPLATE, TABLE_FILTER_PROMPT_TEMPLATE, \
    METRIC_FILTER_PROMPT_TEMPLATE, SQL_GENERATION_PROMPT_TEMPLATE, SQL_CORRECTION_PROMPT_TEMPLATE
from plugins.qdrant import Qdrant
from plugins.es import ES

qdrant = Qdrant()
es = ES()
intention_model = model.with_structured_output(Intention, method="json_mode")


class Node:
    def __init__(self, function: Callable[[State, Runtime], Any]) -> None:
        self.key = function.__name__
        self.function = function
        update_wrapper(self, function)

    async def __call__(self, state: State, runtime: Runtime) -> Any:
        return await self.function(state, runtime)


@Node
async def analyse_intention(state: State, runtime: Runtime):
    writer = runtime.stream_writer
    writer(Step(step="分析用户意图", status=StepStatus.RUNNING))
    try:
        response: Intention = await intention_model.ainvoke([
            SystemMessage(content=INTENTION_ANALYSIS_PROMPT),
            *state.messages
        ])
    except Exception as e:
        logger.error(e)
        writer(Step(step="分析用户意图", status=StepStatus.ERROR, finished=True))
        return Command(goto=END, update={"error": str(e)})

    if not response.continued:
        writer(Step(step="分析用户意图", status=StepStatus.ERROR, finished=True))
        return Command(goto=END)

    writer(Step(step="分析用户意图", status=StepStatus.SUCCESS, guide_questions=response.guide_questions))
    return Command(goto=extract_keywords.key, update={"guide_questions": response.guide_questions})


allowed_pos = (
    "n",
    "nr",
    "ns",
    "nt",
    "nz",
    "v",
    "vn",
    "a",
    "an",
    "eng",
    "i",
    "l",
    "m",
    "t"
)


@Node
async def extract_keywords(state: State, runtime: Runtime):
    writer = runtime.stream_writer
    writer(Step(step="提取关键词", status=StepStatus.RUNNING))
    question = state.messages[-1].content
    keywords = list(set(extract_tags(question, allowPOS=allowed_pos) + [question]))
    writer(Step(step="提取关键词", status=StepStatus.SUCCESS))
    logger.debug(keywords)
    return Command(goto=recall_columns.key, update={"keywords": keywords})


async def search_by_keywords(keywords: list[str], collection_name: str) -> dict[str, Any]:
    values = {}
    payloads = await qdrant.search_points(collection_name, keywords)
    for payload in payloads:
        if not payload:
            continue
        id = payload["id"]
        if id not in values:
            values[id] = payload
    return values


@Node
async def recall_columns(state: State, runtime: Runtime):
    writer = runtime.stream_writer
    writer(Step(step="召回字段信息", status=StepStatus.RUNNING))
    question = state.messages[-1].content
    prompt = COLUMN_KEYWORD_EXTENSION_PROMPT_TEMPLATE.substitute(question=question)
    response = await model.ainvoke([
        SystemMessage(content=prompt),
        *state.messages
    ])

    try:
        keywords = str_list_adapter.validate_json(response.content)
    except ValidationError as e:
        logger.error(e)
        writer(Step(step="召回字段信息", status=StepStatus.ERROR, finished=True))
        return Command(goto=END, update={"error": str(e)})

    keywords = list(set(keywords + state.keywords))
    columns = await search_by_keywords(keywords, "columns")
    writer(Step(step="召回字段信息", status=StepStatus.SUCCESS))
    return Command(goto=recall_metrics.key, update={"columns": [ColumnInfo(**x) for x in columns.values()]})


@Node
async def recall_metrics(state: State, runtime: Runtime):
    writer = runtime.stream_writer
    writer(Step(step="召回指标信息", status=StepStatus.RUNNING))
    question = state.messages[-1].content
    prompt = METRIC_KEYWORD_EXTENSION_PROMPT_TEMPLATE.substitute(question=question)
    response = await model.ainvoke([
        SystemMessage(content=prompt),
        *state.messages
    ])

    try:
        keywords = str_list_adapter.validate_json(response.content)
    except ValidationError as e:
        logger.error(e)
        writer(Step(step="召回指标信息", status=StepStatus.ERROR, finished=True))
        return Command(goto=END, update={"error": str(e)})

    keywords = list(set(keywords + state.keywords))
    metrics = await search_by_keywords(keywords, "metrics")
    writer(Step(step="召回指标信息", status=StepStatus.SUCCESS))
    return Command(goto=recall_values.key, update={"metrics": [MetricInfo(**x) for x in metrics.values()]})


@Node
async def recall_values(state: State, runtime: Runtime):
    writer = runtime.stream_writer
    writer(Step(step="召回值信息", status=StepStatus.RUNNING))
    question = state.messages[-1].content
    prompt = VALUE_KEYWORD_EXTENSION_PROMPT_TEMPLATE.substitute(question=question)
    response = await model.ainvoke([
        SystemMessage(content=prompt),
        *state.messages
    ])

    try:
        keywords = str_list_adapter.validate_json(response.content)
    except ValidationError as e:
        logger.error(e)
        writer(Step(step="召回值信息", status=StepStatus.ERROR, finished=True))
        return Command(goto=END, update={"error": str(e)})

    keywords = list(set(keywords + state.keywords))
    values = await es.search_documents(keywords, "values")
    writer(Step(step="召回值信息", status=StepStatus.SUCCESS))
    return Command(goto=merge_infos.key, update={"values": [ValueInfo(**x) for x in values]})


@Node
async def merge_infos(state: State, runtime: Runtime):
    writer = runtime.stream_writer
    writer(Step(step="合并字段信息", status=StepStatus.RUNNING))

    column_map = {x.id: x for x in state.columns}
    column_ids = set()
    column_ids.update([x.column_id for x in state.values])
    for metric in state.metrics:
        column_ids.update([x for x in metric.relevant_columns if x not in column_map])
    columns = await Column.filter(id__in=column_ids).all()
    column_map.update({x.id: ColumnInfo(**x.serialize()) for x in columns})

    column_groups = {}
    for column in column_map.values():
        table_id = column.table_id
        if table_id not in column_groups:
            column_groups[table_id] = [column]
            continue
        column_groups[table_id].append(column)
    columns = await Column.filter(
        table_id__in=column_groups.keys(),
        role__in=[ColumnRoleOption.PRIMARY_KEY, ColumnRoleOption.FOREIGN_KEY]
    ).all()

    for column_group in column_groups.values():
        column_ids = {x.id for x in column_group}
        for column in columns:
            if column.id in column_ids:
                continue
            column_group.append(ColumnInfo(**column.serialize()))
    table_map = await Table.in_bulk(column_groups.keys(), "id")
    tables = []
    for table_id, table in table_map.items():
        if table_id not in column_groups:
            continue
        tables.append(TableInfo(**table.serialize(), columns=column_groups[table_id]))
    writer(Step(step="合并字段信息", status=StepStatus.SUCCESS))
    return Command(goto=filter_tables.key, update={"columns": list(column_map.values()), "tables": tables})


@Node
async def filter_tables(state: State, runtime: Runtime):
    writer = runtime.stream_writer
    writer(Step(step="过滤表信息", status=StepStatus.RUNNING))
    question = state.messages[-1].content
    prompt = TABLE_FILTER_PROMPT_TEMPLATE.substitute(question=question,
                                                     tables=[x.model_dump() for x in state.tables])
    response = await model.ainvoke([
        SystemMessage(content=prompt),
        *state.messages
    ])

    try:
        table_options = {x: set(y) for x, y in str_key_any_value_dict_adapter.validate_json(response.content).items()}
    except ValidationError as e:
        logger.error(e)
        writer(Step(step="过滤表信息", status=StepStatus.ERROR, finished=True))
        return Command(goto=END, update={"error": str(e)})

    new_tables = []
    for t in state.tables:
        if t.name not in table_options:
            continue
        column_options = table_options[t.name]
        new_columns = []
        for column in t.columns:
            if column.name in column_options:
                new_columns.append(column)
        t.columns = new_columns
        new_tables.append(t)
    writer(Step(step="过滤表信息", status=StepStatus.SUCCESS))
    return Command(goto=filter_metrics.key, update={"tables": new_tables})


@Node
async def filter_metrics(state: State, runtime: Runtime):
    writer = runtime.stream_writer
    writer(Step(step="过滤指标信息", status=StepStatus.RUNNING))
    question = state.messages[-1].content
    prompt = METRIC_FILTER_PROMPT_TEMPLATE.substitute(question=question,
                                                      metrics=[x.model_dump() for x in state.metrics])
    response = await model.ainvoke([
        SystemMessage(content=prompt),
        *state.messages
    ])

    try:
        metric_options = set(str_list_adapter.validate_json(response.content))
    except ValidationError as e:
        logger.error(e)
        writer(Step(step="过滤指标信息", status=StepStatus.ERROR, finished=True))
        return Command(goto=END, update={"error": str(e)})

    new_metrics = []
    for m in state.metrics:
        if m.name in metric_options:
            new_metrics.append(m)
    writer(Step(step="过滤指标信息", status=StepStatus.SUCCESS))
    return Command(goto=add_extra_info.key, update={"metrics": new_metrics})


zone_info = ZoneInfo("Asia/Shanghai")


@Node
async def add_extra_info(state: State, runtime: Runtime):
    writer = runtime.stream_writer
    writer(Step(step="添加额外信息", status=StepStatus.RUNNING))
    db_info = await get_db_info()
    now = datetime.now(zone_info)
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    week_day = now.isoweekday()
    quarter = (now.month - 1) // 3 + 1
    writer(Step(step="添加额外信息", status=StepStatus.SUCCESS))
    return Command(
        goto=generate_sql.key,
        update={"db_info": db_info, "date_info": {"now": now_str, "week_day": week_day, "quarter": quarter}}
    )


@Node
async def generate_sql(state: State, runtime: Runtime):
    writer = runtime.stream_writer
    writer(Step(step="生成SQL", status=StepStatus.RUNNING))
    question = state.messages[-1].content
    prompt = SQL_GENERATION_PROMPT_TEMPLATE.substitute(question=question,
                                                       tables=[x.model_dump() for x in state.tables],
                                                       metrics=[x.model_dump() for x in state.metrics],
                                                       values=[x.model_dump() for x in state.values],
                                                       db_info=state.db_info,
                                                       date_info=state.date_info)
    response = await model.ainvoke([
        SystemMessage(content=prompt),
        *state.messages
    ])

    try:
        sql = str_adapter.validate_strings(response.content)
    except ValidationError as e:
        logger.error(e)
        writer(Step(step="生成SQL", status=StepStatus.ERROR, finished=True))
        return Command(goto=END, update={"error": str(e)})

    writer(Step(step="生成SQL", status=StepStatus.SUCCESS))
    return Command(goto=validate_sql.key, update={"sql": sql})


@Node
async def validate_sql(state: State, runtime: Runtime):
    writer = runtime.stream_writer
    writer(Step(step="校验SQL", status=StepStatus.RUNNING))

    retry_count = state.retry_count
    if retry_count >= 100:
        writer(Step(step="校验SQL", status=StepStatus.ERROR, finished=True))
        return Command(goto=END, update={"error": "SQL is invalid"})

    sql = state.sql
    if not sql:
        writer(Step(step="校验SQL", status=StepStatus.ERROR, finished=True))
        return Command(goto=END, update={"error": "SQL is empty"})
    retry_count += 1

    valid, error = await explain_sql(sql)
    if not valid:
        writer(Step(step="校验SQL", status=StepStatus.ERROR, finished=True))
        return Command(goto=correct_sql.key, update={"error": error, "retry_count": retry_count})

    writer(Step(step="校验SQL", status=StepStatus.SUCCESS))
    return Command(goto=execute_sql.key, update={"error": None, "retry_count": retry_count})


@Node
async def correct_sql(state: State, runtime: Runtime):
    writer = runtime.stream_writer
    writer(Step(step="校正SQL", status=StepStatus.RUNNING))

    question = state.messages[-1].content
    prompt = SQL_CORRECTION_PROMPT_TEMPLATE.substitute(question=question,
                                                       sql=state.sql,
                                                       error=state.error,
                                                       tables=[x.model_dump() for x in state.tables],
                                                       metrics=[x.model_dump() for x in state.metrics],
                                                       date_info=state.date_info,
                                                       db_info=state.db_info)
    response = await model.ainvoke([
        SystemMessage(content=prompt),
        *state.messages
    ])

    try:
        sql = str_adapter.validate_strings(response.content)
    except ValidationError as e:
        logger.error(e)
        writer(Step(step="校正SQL", status=StepStatus.ERROR, finished=True))
        return Command(goto=END, update={"error": str(e)})
    writer(Step(step="校正SQL", status=StepStatus.SUCCESS))
    return Command(goto=execute_sql.key, update={"sql": sql})


@Node
async def execute_sql(state: State, runtime: Runtime):
    writer = runtime.stream_writer
    writer(Step(step="执行SQL", status=StepStatus.RUNNING))

    sql = state.sql
    if not sql:
        writer(Step(step="执行SQL", status=StepStatus.ERROR, finished=True))
        return Command(goto=END, update={"error": "SQL is empty"})

    try:
        data = await execute_raw_sql(sql)
    except Exception as e:
        logger.error(e)
        writer(Step(step="执行SQL", status=StepStatus.ERROR, finished=True))
        return Command(goto=END, update={"error": str(e)})

    writer(Step(step="执行SQL", status=StepStatus.SUCCESS, data=data, finished=True))
    return state
